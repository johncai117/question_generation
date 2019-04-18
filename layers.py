import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

from utils import masked_softmax
import config


class Embedding(nn.Module):
    """Embedding layer used by BiDAF, with Words and Characters.
    Args:
        word_vectors (torch.Tensor): Pre-trained word vectors.
        char_vectors (torch.Tensor): Randomly initialized char vectors
        hidden_size (int): Size of hidden activations.
        drop_prob (float): Probability of zero-ing out activations
    """
    def __init__(self, word_vectors, char_vectors, hidden_size, drop_prob):
        super(Embedding, self).__init__()
        self.drop_prob = drop_prob
        self.w_embed = nn.Embedding.from_pretrained(word_vectors, freeze=True)
        self.c_embed = nn.Embedding.from_pretrained(char_vectors, freeze=False)
        self.proj = nn.Linear(word_vectors.size(1), hidden_size, bias=False)
        self.char_conv = nn.Conv2d(1, config.char_channel_size, (config.char_embedding_size, config.char_channel_width))
        self.hwy = HighwayEncoder(2, hidden_size * 2)

    def forward(self, x, y):
        batch_size = x.size(0)

        w_emb = self.w_embed(x)   # (batch_size, seq_len, embed_size)
        w_emb = F.dropout(w_emb, self.drop_prob, self.training)
        w_emb = self.proj(w_emb)  # (batch_size, seq_len, hidden_size)

        c_emb = self.c_embed(y)
        c_emb = F.dropout(c_emb, self.drop_prob, self.training)
        c_emb = c_emb.view(-1, config.char_embedding_size, c_emb.size(2)).unsqueeze(1)
        c_emb = self.char_conv(c_emb).squeeze()
        c_emb = F.max_pool1d(c_emb, c_emb.size(2)).squeeze()
        c_emb = c_emb.view(batch_size, -1, config.char_channel_size)

        emb = torch.cat([w_emb, c_emb], dim=-1)

        emb = self.hwy(emb)   # (batch_size, seq_len, hidden_size)

        return emb


class HighwayEncoder(nn.Module):
    """Encode an input sequence using a highway network.
    Based on the paper:
    "Highway Networks"
    by Rupesh Kumar Srivastava, Klaus Greff, Jürgen Schmidhuber
    (https://arxiv.org/abs/1505.00387).
    Args:
        num_layers (int): Number of layers in the highway encoder.
        hidden_size (int): Size of hidden activations.
    """
    def __init__(self, num_layers, hidden_size):
        super(HighwayEncoder, self).__init__()
        self.transforms = nn.ModuleList([nn.Linear(hidden_size, hidden_size)
                                         for _ in range(num_layers)])
        self.gates = nn.ModuleList([nn.Linear(hidden_size, hidden_size)
                                    for _ in range(num_layers)])

    def forward(self, x):
        for gate, transform in zip(self.gates, self.transforms):
            # Shapes of g, t, and x are all (batch_size, seq_len, hidden_size)
            g = torch.sigmoid(gate(x))
            t = F.relu(transform(x))
            x = g * t + (1 - g) * x

        return x


class RNNEncoder(nn.Module):
    """General-purpose layer for encoding a sequence using a bidirectional RNN.
    Encoded output is the RNN's hidden state at each position, which
    has shape `(batch_size, seq_len, hidden_size * 2)`.
    Args:
        input_size (int): Size of a single timestep in the input.
        hidden_size (int): Size of the RNN hidden state.
        num_layers (int): Number of layers of RNN cells to use.
        drop_prob (float): Probability of zero-ing out activations.
    """
    def __init__(self,
                 input_size,
                 hidden_size,
                 num_layers,
                 drop_prob=0.):
        super(RNNEncoder, self).__init__()
        self.drop_prob = drop_prob
        self.rnn = nn.LSTM(input_size, hidden_size, num_layers,
                           batch_first=True,
                           bidirectional=True,
                           dropout=drop_prob if num_layers > 1 else 0.)

    def forward(self, x, lengths):
        # Save original padded length for use by pad_packed_sequence
        orig_len = x.size(1)

        # Sort by length and pack sequence for RNN
        lengths, sort_idx = lengths.sort(0, descending=True)
        x = x[sort_idx]     # (batch_size, seq_len, input_size)
        x = pack_padded_sequence(x, lengths, batch_first=True)

        # Apply RNN
        x, (hidden, cell) = self.rnn(x)  # (batch_size, seq_len, 2 * hidden_size)

        # Unpack and reverse sort
        x, _ = pad_packed_sequence(x, batch_first=True, total_length=orig_len)
        _, unsort_idx = sort_idx.sort(0)

        x = x[unsort_idx]  # (batch_size, seq_len, 2 * hidden_size)
        hidden = hidden[:, unsort_idx, :]
        cell = cell[:, unsort_idx, :]

        # Apply dropout (RNN applies dropout after all but the last layer)
        x = F.dropout(x, self.drop_prob, self.training)

        return x, (hidden, cell)


class BiDAFAttention(nn.Module):
    """Bidirectional attention originally used by BiDAF.
    Bidirectional attention computes attention in two directions:
    The context attends to the query and the query attends to the context.
    The output of this layer is the concatenation of [context, c2q_attention,
    context * c2q_attention, context * q2c_attention]. This concatenation allows
    the attention vector at each timestep, along with the embeddings from
    previous layers, to flow through the attention layer to the modeling layer.
    The output has shape (batch_size, context_len, 8 * hidden_size).
    Args:
        hidden_size (int): Size of hidden activations.
        drop_prob (float): Probability of zero-ing out activations.
    """
    def __init__(self, hidden_size, drop_prob=0.1):
        super(BiDAFAttention, self).__init__()
        self.drop_prob = drop_prob
        self.c_weight = nn.Parameter(torch.zeros(hidden_size, 1))
        self.q_weight = nn.Parameter(torch.zeros(hidden_size, 1))
        self.cq_weight = nn.Parameter(torch.zeros(1, 1, hidden_size))
        for weight in (self.c_weight, self.q_weight, self.cq_weight):
            nn.init.xavier_uniform_(weight)
        self.bias = nn.Parameter(torch.zeros(1))

    def forward(self, c, q, c_mask, q_mask):
        batch_size, c_len, _ = c.size()
        q_len = q.size(1)
        s = self.get_similarity_matrix(c, q)        # (batch_size, c_len, q_len)
        c_mask = c_mask.view(batch_size, c_len, 1)  # (batch_size, c_len, 1)
        q_mask = q_mask.view(batch_size, 1, q_len)  # (batch_size, 1, q_len)
        s1 = masked_softmax(s, q_mask, dim=2)       # (batch_size, c_len, q_len)
        s2 = masked_softmax(s, c_mask, dim=1)       # (batch_size, c_len, q_len)

        # (bs, c_len, q_len) x (bs, q_len, hid_size) => (bs, c_len, hid_size)
        a = torch.bmm(s1, q)
        # (bs, c_len, c_len) x (bs, c_len, hid_size) => (bs, c_len, hid_size)
        b = torch.bmm(torch.bmm(s1, s2.transpose(1, 2)), c)
        x = torch.cat([c, a, c * a, c * b], dim=2)  # (bs, c_len, 4 * hid_size)
        #x = torch.add(a, b)  # (bs, c_len, hid_size) --> we added the two context vectors

        return x

    def get_similarity_matrix(self, c, q):
        """Get the "similarity matrix" between context and query (using the
        terminology of the BiDAF paper).
        A naive implementation as described in BiDAF would concatenate the
        three vectors then project the result with a single weight matrix. This
        method is a more memory-efficient implementation of the same operation.
        See Also:
            Equation 1 in https://arxiv.org/abs/1611.01603
        """
        c_len, q_len = c.size(1), q.size(1)
        c = F.dropout(c, self.drop_prob, self.training)  # (bs, c_len, hid_size)
        q = F.dropout(q, self.drop_prob, self.training)  # (bs, q_len, hid_size)

        # Shapes: (batch_size, c_len, q_len)
        s0 = torch.matmul(c, self.c_weight).expand([-1, -1, q_len])
        s1 = torch.matmul(q, self.q_weight).transpose(1, 2)\
                                           .expand([-1, c_len, -1])
        s2 = torch.matmul(c * self.cq_weight, q.transpose(1, 2))
        s = s0 + s1 + s2 + self.bias

        return s


class Encoder(nn.Module):
    def __init__(self, input_dim, emb_dim, hid_dim, n_layers, dropout):
        super().__init__()

        self.input_dim = input_dim
        self.emb_dim = emb_dim
        self.hid_dim = hid_dim
        self.n_layers = n_layers
        self.dropout = dropout

        self.embedding = nn.Embedding(input_dim, emb_dim)

        self.rnn = nn.LSTM(emb_dim, hid_dim, n_layers, batch_first=True, bidirectional=True, dropout=dropout)

        self.dropout = nn.Dropout(dropout)

    def forward(self, src):

        # src = [batch size, src sent len, ]

        embedded = self.dropout(self.embedding(src))

        # embedded = [batch size, src sent len, emb dim]

        outputs, (hidden, cell) = self.rnn(embedded)

        # outputs = [batch size, src sent len, hid dim * n directions]
        # hidden = [batch size, n layers * n directions, hid dim]
        # cell = [batch size, n layers * n directions, hid dim]

        # outputs are always from the top hidden layer

        return hidden, cell


class Decoder(nn.Module):
    def __init__(self, input_size, output_dim, word_vectors, hidden_size, n_layers, dropout):
        super().__init__()

        self.n_layers = n_layers
        self.dropout = dropout

        self.embedding = nn.Embedding.from_pretrained(word_vectors, freeze=True)

        self.rnn = nn.LSTM(input_size, hidden_size, n_layers, batch_first=True, bidirectional=False, dropout=dropout)

        self.out = nn.Linear(hidden_size, output_dim)

        self.dropout = nn.Dropout(dropout)

    def forward(self, input, hidden, cell):
        # input = [batch size]
        # hidden = [n layers * n directions, batch size, hid dim]
        # cell = [n layers * n directions, batch size, hid dim]

        # n directions in the decoder will both always be 1, therefore:
        # hidden = [n layers, batch size, hid dim]
        # context = [n layers, batch size, hid dim]

        input = input.unsqueeze(1)   #  (batch size, 1)

        embedded = self.dropout(self.embedding(input))   # (batch size, 1, emb dim)

        output, (hidden, cell) = self.rnn(embedded, (hidden, cell))

        # output = [batch size, sent len, hid dim * n directions]
        # hidden = [batch size, n layers * n directions, hid dim]
        # cell = [batch size, n layers * n directions, hid dim]

        # sent len and n directions will always be 1 in the decoder, therefore:
        # output = [batch size, 1, hid dim]
        # hidden = [batch size, n layers, hid dim]
        # cell = [batch size, n layers, hid dim]

        prediction = self.out(output.squeeze(1))

        # prediction = [batch size, output dim]

        # Softmax of prediction?
        softmax_fn = F.log_softmax # if log_softmax else F.softmax
        probs = softmax_fn(prediction, dim=1)

        return probs, hidden, cell
