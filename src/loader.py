import torch
from torchtext.vocab import Vectors

import os, re, nltk, random
from cached_property import cached_property

sent_tokenizer = nltk.data.load('tokenizers/punkt/english.pickle')


class Batch:
    """ Contains many documents for efficient computation """
    def __init__(self, documents):
        self.documents = documents
        self.ix, self.sents, self.labels = self.unravel(documents)
    
    def __getitem__(self, idx):
        return self.sents[idx]

    def __repr__(self):
        return ('Batch of %d sentences from %d documents' 
                % (len(self.sents), len(self.documents)))
    
    def __len__(self):
        return len(self.sents)
    
    def unravel(self, documents):
        lengths = [0] + [len(d) for d in documents]
        ix = [sum(lengths[:idx+1]) for idx, _ in enumerate(lengths)]
        sents = flatten([d.sents for d in documents])
        labels = torch.cat([d.labels for d in documents])
        return ix, sents, labels
        
    def regroup(self, groups):
        regrouped = [groups[self.ix[i]:self.ix[i+1]] for i in range(len(self.ix)-1)]
        return regrouped


class Document:
    """ Contains all sentences in a Wiki article, whether they end a 
    subsection, and the document's filename """
    def __init__(self, sentences, labels, filename):
        self.sents = sentences
        self.labels = torch.tensor(labels)
        self.filename = filename
    
    def __getitem__(self, idx):
        return self.sents[idx]

    def __repr__(self):
        return 'Document containing %d sentences' % (len(self.sents))
    
    def __len__(self):
        return len(self.sents)


class Sentence:
    """ Contain text, tokenized text, and label of a sentence """
    def __init__(self, text, label):
        self.text = text
        self.tokens = [clean_token(t) for t in text.split()]
        self.label = label
        
    def __getitem__(self, idx):
        return self.tokens[idx]
        
    def __repr__(self):
        return '"' + self.text + '"'
    
    def __len__(self):
        return len(self.tokens)


class LazyVectors:
    """Load only those vectors from GloVE that are in the vocab."""
    
    unk_idx = 1

    def __init__(self, name='glove.840B.300d.txt', skim=None):
        """ Requires the glove vectors to be in a folder named .vector_cache
        
        In Bash/Terminal from directory this class is in:
        >> mkdir .vector_cache
        >> mv glove.840B.300d.txt .vector_cache/glove.840B.300d.txt
        """
        self.name = name
        self.skim = skim # For debugging; loads faster.
        self.set_vocab()

    @cached_property
    def loader(self):
        return Vectors(self.name)
    
    def get_vocab(self, filename='vocabulary.txt'):
        """ Read in vocabulary (top 30K words, covers ~93.5% of all tokens) """ 
        with open(filename, 'r') as f:
            vocab = f.read().split(',')
        return vocab
    
    def set_vocab(self):
        """Set corpus vocab """
        # Intersect with model vocab.
        self.vocab = [v for v in self.get_vocab()
                      if v in self.loader.stoi][:self.skim]

        # Map string -> intersected index.
        self._stoi = {s: i for i, s in enumerate(self.vocab)}

    def weights(self):
        """Build weights tensor for embedding layer """
        # Select vectors for vocab words.
        weights = torch.stack([
            self.loader.vectors[self.loader.stoi[s]]
            for s in self.vocab
        ])

        # Padding + UNK zeros rows.
        return torch.cat([
            torch.zeros((2, self.loader.dim)),
            weights,
        ])

    def stoi(self, s):
        """ String to index (s to i) for embedding lookup """
        idx = self._stoi.get(s)
        return idx + 2 if idx else self.unk_idx


def crawl_directory(dirname):
    """ Walk a nested directory to get all filename ending in a pattern """
    filenames = []
    for path, subdirs, files in os.walk(dirname):
        for name in files:
            if not name.endswith('.DS_Store'):
                yield os.path.join(path, name)
                
def sample_and_batch(*args, TRAIN):
    """ Sample some directory path and Batch the documents """
    files = sample_nested_dir(*args)
    documents = [read_document(f, TRAIN) for f in files]
    return Batch(documents)
                
def sample_and_read(*args, TRAIN):
    """ Sample some directory path for batch_size number of documents """
    files = sample_nested_dir(*args)
    for f in files:
        yield read_document(f, TRAIN)
                
def sample_nested_dir(directory, batch_size=100):
    """ Sample files from a nested directory """
    samples, dirname = [], directory
    while len(samples) < batch_size:
        try:
            files = safe_listdir(dirname)
            subdir = random.sample(files, 1)[0]
            dirname = os.path.join(dirname, subdir)
        except NotADirectoryError:
            samples.append(dirname)
            dirname = directory
            
    return samples[:batch_size]

def safe_listdir(dirname):
    """ Listdir but without .DS_Store hidden file """
    return [f for f in os.listdir(dirname) if not f.endswith('.DS_Store')]

def read_document(filename, TRAIN, minlen=1):
    """ Read in a Wiki-727 file. 
    Only keep documents longer than minlen subsections. """
    
    # Initialize, open file
    document, subsection = [], ''
    with open(filename) as f:
        
        # For each line in the file
        for line in f.readlines()[1:]:
            
            # This '========' indicates a new subsection
            if line.startswith('========'):
                document.append(sent_tokenizer.tokenize(subsection.strip()))
                subsection = ''
            else:
                subsection += ' ' + line[:-1] # Exclude '\n' from the line
        
        # Edge case of last subsection needs to be appended
        document.append(sent_tokenizer.tokenize(subsection.strip()))
    
    # Keep only subsections longer than minlen
    document = [subsection for subsection in document if len(subsection) >= minlen]
    
    # As per original paper, remove first subsection during training
    if TRAIN:
        document = document[1:]
    
    # Compute labels for the subsections
    labels = doc_to_labels(document)

    # Organize the data into objects, defined above.
    document = Document([Sentence(text, label) for text, label in zip(flatten(document), labels)], 
                         labels,
                         filename)
    
    return document

def doc_to_labels(document):
    """ Convert Wiki-727 file to labels 
    (last sentence of a subsection is 1, otherwise 0) """
    return flatten([(len(doc)-1)*[0] + [1] for doc in document])

def clean_token(token):
    """ Remove everything but whitespace, the alphabet, digits; 
    separate apostrophes for stopwords; replace only digit tokens """
    if token.isdigit():
        token = '#NUM'
    else:
        token = re.sub(r"[^a-z0-9\s]", '', token.lower())
        token = re.sub(r"[']+", ' ', token)
    return token

def chunk_list(alist, n):
    """ Yield successive alist into len(alist)/n chunks of size n """
    for i in range(0, len(alist), n):
        yield alist[i:i+n]

def flatten(alist):
    """ Flatten a list of lists into one list """
    return [item for sublist in alist for item in sublist]