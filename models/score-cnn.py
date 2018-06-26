import torch
import torch.optim as optim
import torch.nn as nn
import torch.nn.functional as F

import numpy as np
import matplotlib.pyplot as plt

from collections import defaultdict
from copy import deepcopy
from tqdm import tqdm, tqdm_notebook

from loader import *
from utils import *
from metrics import Metrics, avg_dicts

# Load in corpus, lazily load in word vectors.
VECTORS = LazyVectors()

def token_to_id(token):
    """ Lookup word ID for a token """
    return VECTORS.stoi(token)

def sent_to_tensor(sent):
    """ Convert a sentence to a lookup ID tensor """
    return torch.tensor([token_to_id(t) for t in sent.tokens])


class CNNEncoder(nn.Module):
    """ CNN over a Batch of variable length sentences padded 
    or truncated to maxlen. maxpool over
    each sentence's hidden states to get its representation. 
    """
        
    def __init__(self, hidden_dim, drop_prob, maxlen, nrange, method):
        super().__init__()
        
        assert type(nrange) == list, 'Argument "nrange" is a list of token convolution sizes.'
        assert method in ['avg', 'last', 'max', 'sum'], 'Invalid method chosen.'
        self.method = eval('self._'+ method)
        
        weights = VECTORS.weights()
        
        self.embeddings = nn.Embedding(weights.shape[0], weights.shape[1], padding_idx=0)
        self.embeddings.weight.data.copy_(weights)
                
        self.convs = nn.ModuleList([nn.Conv1d(in_channels=weights.shape[1], 
                                              out_channels=hidden_dim,
                                              kernel_size=n)
                                    for n in nrange])
        
        self.drop = drop_prob
        self.maxlen = maxlen
    
    def forward(self, batch):
        
        # Convert sentences to embed lookup ID tensors
        sent_tensors = [sent_to_tensor(s).unsqueeze(1) for s in batch]
        
        # Pad to maximum length sentence in the batch
        padded, _ = pad_and_stack(sent_tensors, pad_size=self.maxlen)
        
        # Embed tokens in each sentence, apply dropout, transpose for input to CNN
        embedded = F.dropout(self.embeddings(padded), 0.20).squeeze().transpose(1,2)
    
        # Convolve over words
        convolved = [conv(embedded) for conv in self.convs]
        
        # Cat together convolutions
        catted = torch.cat(convolved, dim=2)
        
        # Squash down a dimension
        representation = self.method(catted)
        
        return representation
    
    def _avg(self, catted):
        """ Average token states """
        return catted.mean(dim=1)
        
    def _max(self, catted):
        """ Maxpool token states """
        return F.max_pool2d(catted, kernel_size=(catted.shape[1], 1)).squeeze()
        
    def _sum(self, catted):
        """ Sum token states """
        return catted.sum(dim=1)


class Score(nn.Module):
    """ Take outputs from encoder, produce probabilities for each
    sentence that it ends a segment. 
    """
    def __init__(self, input_dim, hidden_dim, out_dim, drop_prob):
        super().__init__()
        
        self.score = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.Dropout(drop_prob),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(), 
            nn.Dropout(drop_prob),
            nn.Linear(hidden_dim, out_dim),
        )

    def forward(self, higher_output):
        return self.score(higher_output)


class CNNScore(nn.Module):
    """ Super class for taking an input batch of sentences from a Batch
    and computing the probability whether they end a segment or not 
    """
    def __init__(self, hidden_dim, score_dim, drop_prob, maxlen, nrange, method):
        super().__init__()
                
        # Compute input dimension size for Score
        input_dim = sum([maxlen - (n-1) for n in nrange])
        
        # Chain modules together to get overall model
        self.model = nn.Sequential(
            CNNEncoder(hidden_dim, drop_prob, maxlen, nrange, method),
            Score(input_dim, score_dim, out_dim=2, drop_prob=drop_prob)
        )
        
    def forward(self, batch):
        return self.model(batch)


class Trainer:
    """ Class to train, validate, and test a model
    """
    # Progress logging, initialization of metrics
    train_loss = []
    val_loss = [] 
    metrics = defaultdict(list)
    best_val = 1e10
    evalu = Metrics()
    
    def __init__(self, model, train_dir, val_dir, test_dir=None, 
                 lr=1e-3, batch_size=10):
        
        self.model = model
        self.optimizer = optim.Adam(params=[p for p in self.model.parameters() 
                                            if p.requires_grad], 
                                    lr=lr)

        self.batch_size = batch_size
        
        self.train_dir = train_dir
        self.val_dir = val_dir
        self.test_dir = test_dir if test_dir else val_dir
            
    def train(self, num_epochs, *args, **kwargs):
        """ Train a model """
        for epoch in range(1, num_epochs+1):
            self.train_epoch(epoch, *args, **kwargs)
    
    def train_epoch(self, epoch, steps=25, val_ckpt=5, visualize=True):
        """ Train the model for one epoch """
        self.val_ckpt = val_ckpt
        
        # Enable dropout, any learnable regularization
        self.model.train()
        
        epoch_loss, epoch_sents = [], []
        for step in tqdm_notebook(range(1, steps+1)):
            
            # Zero out gradients
            self.optimizer.zero_grad()
            
            # Compute a train batch, backpropagate
            batch_loss, num_sents, segs_correct, total_segs = self.train_batch()
            batch_loss.backward()
            
            # Log progress (Loss is reported as average loss per sentence)
            print('Step: %d | Loss: %f | Num. sents: %d | Segs correct: %d / %d'
                 % (step, batch_loss.item()/num_sents, num_sents, segs_correct, total_segs))
            
            # For logging purposes
            epoch_loss.append(batch_loss.item())
            epoch_sents.append(num_sents)
            
            # Step the optimizer
            self.optimizer.step()
        
        epoch_loss = np.mean(epoch_loss)
        epoch_sents = np.mean(epoch_sents)
        
        # Log progress (Loss is reported as average loss per sentence)
        print('\nEpoch: %d | Loss: %f | Avg. num sents: %d\n' 
              % (epoch, epoch_loss/epoch_sents, epoch_sents))
        
        self.train_loss.append(epoch_loss/epoch_sents)
        
        # Validation set performance
        if epoch % val_ckpt == 0:
            
            metrics_dict, val_loss = self.validate(self.val_dir)
            
            # Log progress
            self.val_loss.append(val_loss)
            for key, val in metrics_dict.items():
                self.metrics[key].append(val)
                            
            if val_loss < self.best_val:
                self.best_val = val_loss
                self.best_model = deepcopy(self.model.eval())
            
            # Log progress
            print('Validation loss: %f | Best val loss: %f\n' 
                  % (val_loss, self.best_val))
            
            if visualize:
                self.viz()

    def train_batch(self):
        """ Train the model using one batch """
        
        # Sample a batch of documents
        batch = sample_and_batch(self.train_dir, self.batch_size, TRAIN=True)

        # Get predictions for each document in the batch
        preds = self.model(batch)

        # Compute loss, IGNORING last entry as it ALWAYS ends a subsection
        batch_loss = F.cross_entropy(preds[:-1], batch.labels[:-1], size_average=False)
        
        # Number of boundaries correctly predicted
        segs_correct, total_segs = self.debugging(preds, batch)
        
        return batch_loss, len(batch), segs_correct, total_segs
    
    def debugging(self, preds, batch):
        """ Check how many segment boundaries were correctly predicted """
        labels = batch.labels
        logits = F.softmax(preds, dim=1)
        probs, outputs = torch.max(logits, dim=1)
        segs_correct = sum([1 for i,j in zip(batch.labels, outputs)
                            if i == j == torch.tensor(1)])
        total_segs = sum(batch.labels).item()
        
        print('\nBoundary Probabilities:\n')
        print([(logit[1].item(), label.item()) 
               for logit, label in zip(logits, batch.labels)
               if label == 1])
        
        return segs_correct, total_segs
    
    def validate(self, dirname):
        """ Evaluate using SegEval text segmentation metrics """
        
        print('Evaluating across SegEval metrics.')
        
        # Disable dropout, any learnable regularization
        self.model.eval()
        
        # Initialize val directory files, dictionaries list
        files, dicts_list = list(crawl_directory(dirname)), []
        
        eval_loss, num_sents = 0, 0
        # Break into chunks for memory constraints
        for chunk in chunk_list(files, self.batch_size):
            
            # Batchify documents
            batch = Batch([read_document(f, TRAIN=False) for f in chunk])
            
            # Predict the batch
            preds, logits = self.predict_batch(batch)
            
            # Compute validation loss, add number of sentences
            eval_loss += F.cross_entropy(logits, batch.labels, size_average=False)
            num_sents += len(batch)
            
            # Evaluate across SegEval metrics
            metric_dict = self.evalu(batch, preds)
            
            # Save the batch performance
            dicts_list.append(metric_dict)

        # Average dictionaries
        eval_metrics = avg_dicts(dicts_list)
        
        # Normalize eval loss
        normd_eval_loss = eval_loss.item() / num_sents
        
        return eval_metrics, normd_eval_loss
    
    def predict(self, document):
        """ Given a document, predict segmentations """
        return self.predict_batch(Batch([document]))

    def predict_batch(self, batch, THETA=0.50):
        """ Given a batch, predict segmentation boundaries thresholded 
        by min probability THETA, which needs to be tuned """
        
        # Predict
        logits = self.model(batch)
        
        # Softmax for probabilities
        probs = F.softmax(logits, dim=1)
        
        # If greater than threshold theta, make it a boundary
        boundaries = probs[:, 1] > THETA
        
        # Convert from tensor to list
        preds = boundaries.tolist()
        
        return preds, logits

    def save_model(self, savepath):
        """ Save model state dictionary """
        torch.save(self.model.state_dict(), savepath + '.pth')

    def load_model(self, loadpath):
        """ Load state dictionary into model """
        state = torch.load(loadpath)
        self.model.load_state_dict(state)
        self.model = to_cuda(self.model)
        
    def viz(self):
        """ Visualize progress: train loss, val loss, word- sent-level metrics """
        
        # Initialize plot
        _, axes = plt.subplots(ncols=2, nrows=2, sharex='col', sharey='col')
        val, word, train, sent = axes.ravel()

        # Plot validation loss
        val.plot(self.val_loss, c='g')
        val.set_ylabel('Val Loss')
        val.set_ylim([0,1])

        # Plot training loss
        train.plot(self.train_loss, c='r')
        train.set_ylabel('Train Loss')
        
        for key, values in self.metrics.items():
            
            # Plot word-level metrics
            if key.startswith('w_'):
                word.plot(values, label=key)
                
            # Plot sent-level metrics
            elif key.startswith('s_'):
                sent.plot(values, label=key)

        # Fix y axis limits, y label, legend for word-level metrics
        word.set_ylim([0,1])
        word.set_ylabel('Word metrics')
        word.legend(bbox_to_anchor=(1.04,0.5), loc="center left", borderaxespad=0)
        
        # Fix again but this time for sent-level
        sent.set_ylabel('Sent metrics')
        sent.legend(bbox_to_anchor=(1.04,0.5), loc="center left", borderaxespad=0)

        # Give the plots some room to breathe
        plt.subplots_adjust(left=None, bottom=4, right=2, top=5,
                            wspace=None, hspace=None)

        # Display the plot
        plt.show()


# Original paper does 10 epochs across full dataset
model = CNNScore(hidden_dim=256, 
                 score_dim=256, 
                 drop_prob=0.20, 
                 maxlen=64, 
                 nrange=[3,4,5], 
                 method='max')

trainer = Trainer(model=model,
                  train_dir='../data/wiki_727/train', 
                  val_dir='../data/wiki_50/test',
                  batch_size=8,
                  lr=1e-3)

trainer.train(num_epochs=100, 
              steps=25,
              val_ckpt=1)
