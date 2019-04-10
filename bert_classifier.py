import argparse
import torch
import csv
import numpy as np
from torch.utils.data import TensorDataset, DataLoader, SequentialSampler
from pytorch_pretrained_bert.tokenization import BertTokenizer
from pytorch_pretrained_bert.modeling import BertModel, BertConfig
from pytorch_pretrained_bert.optimization import BertAdam
from tqdm import tqdm
import matplotlib
import pandas as pd

matplotlib.use('Agg')
import matplotlib.pyplot as plt


class BertForClassification(torch.nn.Module):
    """BERT model for classification.
    This module is composed of the BERT model with a linear layer on top of
    the pooled output.
    """

    def __init__(self, params, num_labels):
        super(BertForClassification, self).__init__()
        self.num_labels = num_labels
        self.lstm = torch.nn.LSTM(768, params.hidden_dim)
        self.dropout = torch.nn.Dropout(params.dropout)
        # Hidden size of bert base model = 768
        self.classifier = torch.nn.Linear(params.hidden_dim, num_labels)

    def forward(self, input_features):
        embeds = input_features.permute(1, 0, 2)  # seq_len * batch_size * embedding_dim
        _, (hn, cn) = self.lstm(embeds)
        output = hn[-1]                           # bs * hidden_dim
        logits = self.classifier(output)
        return logits


class InputExample(object):

    def __init__(self, unique_id, text_a, text_b):
        self.unique_id = unique_id
        self.text_a = text_a
        self.text_b = text_b


class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self, tokens, input_ids, input_mask, input_type_ids):
        self.tokens = tokens
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.input_type_ids = input_type_ids


def convert_examples_to_features(examples, seq_length, tokenizer):
    """Loads a data file into a list of `InputBatch`s."""

    features = []
    for example in examples:
        tokens_a = tokenizer.tokenize(example)
        # Account for [CLS] and [SEP] with "- 2"
        if len(tokens_a) > seq_length - 2:
            tokens_a = tokens_a[0:(seq_length - 2)]

        # The convention in BERT is:
        # (a) For sequence pairs:
        #  tokens:   [CLS] is this jack ##son ##ville ? [SEP] no it is not . [SEP]
        #  type_ids:   0   0  0    0    0     0      0   0    1  1  1   1  1   1
        # (b) For single sequences:
        #  tokens:   [CLS] the dog is hairy . [SEP]
        #  type_ids:   0   0   0   0  0     0   0
        #
        # Where "type_ids" are used to indicate whether this is the first
        # sequence or the second sequence. The embedding vectors for `type=0` and
        # `type=1` were learned during pre-training and are added to the wordpiece
        # embedding vector (and position vector). This is not *strictly* necessary
        # since the [SEP] token unambigiously separates the sequences, but it makes
        # it easier for the model to learn the concept of sequences.
        #
        # For classification tasks, the first vector (corresponding to [CLS]) is
        # used as as the "sentence vector". Note that this only makes sense because
        # the entire model is fine-tuned.
        tokens = []
        input_type_ids = []
        tokens.append("[CLS]")
        input_type_ids.append(0)
        for token in tokens_a:
            tokens.append(token)
            input_type_ids.append(0)
        tokens.append("[SEP]")
        input_type_ids.append(0)
        input_ids = tokenizer.convert_tokens_to_ids(tokens)

        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.
        input_mask = [1] * len(input_ids)

        # Zero-pad up to the sequence length.
        while len(input_ids) < seq_length:
            input_ids.append(0)
            input_mask.append(0)
            input_type_ids.append(0)

        assert len(input_ids) == seq_length
        assert len(input_mask) == seq_length
        assert len(input_type_ids) == seq_length

        features.append(
            InputFeatures(
                tokens=tokens,
                input_ids=input_ids,
                input_mask=input_mask,
                input_type_ids=input_type_ids))
    return features


def _truncate_seq_pair(tokens_a, tokens_b, max_length):
    """Truncates a sequence pair in place to the maximum length."""

    # This is a simple heuristic which will always truncate the longer sequence
    # one token at a time. This makes more sense than truncating an equal percent
    # of tokens from each, since if one sequence is very short then each token
    # that's truncated likely contains more information than a longer sequence.
    while True:
        total_length = len(tokens_a) + len(tokens_b)
        if total_length <= max_length:
            break
        if len(tokens_a) > len(tokens_b):
            tokens_a.pop()
        else:
            tokens_b.pop()


def read_examples(filename, max_seq_length):
    """Read a list of `InputExample`s from an input file."""
    examples = []
    with open(filename, "r") as f:
        readCSV = csv.reader(f, delimiter=',')
        csv.field_size_limit(100000000)
        for tag, words in readCSV:
            tag = int(tag)
            if tag in [1, 4]:
                # Adjust the tag to {0: Satire, 1: Trusted}
                examples.append((words.lower()[:max_seq_length], tag - 1 if tag == 1 else tag - 3))
    return examples


def read_testset(filename, max_seq_length):
    df = pd.read_excel(filename)
    data = []
    for row in df.values:
        tag = int(row[0])
        # Tag id is reversed in this dataset
        data.append((row[2].lower()[:max_seq_length], tag + 1 if tag == 0 else tag - 1))
    return data


def get_dev_loss_and_acc(model, loss_fn, dev_data_loader, device):
    losses = []
    hits = 0
    total = 0
    model.eval()
    for input_features, input_labels in dev_data_loader:
        logits = model(input_features)
        loss = loss_fn(logits, input_labels)
        hits += torch.sum(torch.argmax(logits, dim=1) == input_labels).item()
        total += len(input_features)
        losses.append(loss.item())

    return np.asscalar(np.mean(losses)), hits / total


class ClassificationDataSet(torch.utils.data.TensorDataset):
    def __init__(self, features, labels, params, bert_model, device):
        super(ClassificationDataSet, self).__init__()
        self.params = params
        # data is a list of tuples (sent, label)
        self.features = features
        self.labels = labels
        self.bert = bert_model
        self.device = device
        self.num_of_samples = len(self.features)

    def __len__(self):
        return self.num_of_samples

    def __getitem__(self, idx):
        input_features = self.features[idx]
        input_ids = torch.tensor([f.input_ids for f in input_features], dtype=torch.long).to(self.device)
        input_mask = torch.tensor([f.input_mask for f in input_features], dtype=torch.long).to(self.device)
        output, _ = self.bert(input_ids, None, input_mask, output_all_encoded_layers=False)
        # Picking the output corresponding to [CLS]
        return output[:, 0, :], torch.tensor(self.labels[idx], dtype=torch.long).to(self.device)

    def collate(self, batch):
        features = [x[0] for x in batch]
        labels = [x[1] for x in batch]
        # Find the longest sentence length in the batch
        max_length = max([f.size(0) for f in features])
        padded_tensor = torch.zeros(len(features), max_length, 768).to(self.device)
        for i, f in enumerate(features):
            padded_tensor[i, :f.size(0), :] = f

        return padded_tensor, torch.stack(labels)


def get_data_loader(args, examples, tokenizer, bert_model, device):
    all_features = []
    all_labels = []
    for example, tag in examples:
        sents = example.split(".")
        features = convert_examples_to_features(
            examples=sents, seq_length=args.max_sent_length, tokenizer=tokenizer)
        all_features.append(features)
        all_labels.append(tag)

    dataset_train = ClassificationDataSet(all_features, all_labels, args, bert_model, device)
    kwargs = {}
    train_data_loader = torch.utils.data.DataLoader(dataset_train, batch_size=args.batch_size,
                                                    collate_fn=dataset_train.collate, shuffle=True, **kwargs)
    return train_data_loader


def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--train", dest="train", type=str, default='data/fulltrain.csv')
    parser.add_argument("--dev", dest="dev", type=str, default='data/balancedtest.csv')
    parser.add_argument("--test", dest="test", type=str, default='data/test.xlsx', help='Out of domain test set')
    parser.add_argument("--bert_model", default="bert-base-uncased", type=str,
                        help="Bert pre-trained model selected in the list: bert-base-uncased, "
                             "bert-large-uncased, bert-base-cased, bert-base-multilingual, bert-base-chinese.")

    ## Other parameters
    parser.add_argument("--do_lower_case", action='store_true', help="Set this flag if you are using an uncased model.")
    parser.add_argument("--layers", default="-1,-2,-3,-4", type=str)
    parser.add_argument("--dropout", dest="dropout", type=float, default=0.2)
    parser.add_argument("--max_epochs", dest="max_epochs", type=int, default=20)
    parser.add_argument("--hidden_dim", dest="hidden_dim", type=int, default=100)
    parser.add_argument("--lr", dest="lr", type=float, default=1e-3)
    parser.add_argument("--config", dest="config", type=str, default='bert', help='Name for saving plots')
    parser.add_argument("--max_seq_length", default=500, type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. Sequences longer "
                             "than this will be truncated, and sequences shorter than this will be padded.")
    parser.add_argument("--max_sent_length", default=50, type=int)
    parser.add_argument("--weight_decay", dest="weight_decay", type=float, default=1e-5)
    parser.add_argument("--batch_size", default=2, type=int, help="Batch size for predictions.")
    parser.add_argument("--mode", dest="mode", type=int, default=0, help='0: train, 1:test')
    parser.add_argument("--model_file", dest="model_file", type=str, default='model_bert.t7', help='For evaluating a '
                                                                                                   'saved model')
    args = parser.parse_args()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    tokenizer = BertTokenizer.from_pretrained(args.bert_model, do_lower_case=args.do_lower_case)

    # Create bert model
    bert_model = BertModel.from_pretrained(args.bert_model)
    bert_model.to(device)

    if args.mode == 0:

        print("Preparing data...")
        train_examples = read_examples(args.train, args.max_seq_length)
        train_dataloader = get_data_loader(args, train_examples, tokenizer, bert_model, device)

        dev_examples = read_examples(args.dev, args.max_seq_length)
        dev_dataloader = get_data_loader(args, dev_examples, tokenizer, bert_model, device)
        print("Preparing data...[OK]")

        model = BertForClassification(args, 2)
        model.to(device)

        loss_fn = torch.nn.CrossEntropyLoss()
        optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

        # Variables for plotting
        train_losses = []
        dev_losses = []
        train_accs = []
        dev_accs = []
        prev_best = 0
        patience = 0

        # Training epoch
        # Start the training loop
        for epoch in range(1, args.max_epochs + 1):
            model.train()
            train_loss = 0
            hits = 0
            total = 0
            for input_features, input_labels in tqdm(train_dataloader):

                logits = model(input_features)
                loss = loss_fn(logits, input_labels)

                # Book keeping
                train_loss += loss.item()
                hits += torch.sum(torch.argmax(logits, dim=1) == input_labels).item()
                # One can alternatively do this accuracy computation on cpu by,
                # moving the logits to cpu: logits.data.cpu().numpy(), and then using numpy argmax.
                # However, we should always avoid moving tensors between devices if possible for faster computation
                total += len(input_features)

                # Back-prop
                optimizer.zero_grad()  # Reset the gradients
                loss.backward()  # Back propagate the gradients
                optimizer.step()  # Update the network

            # Compute loss and acc for dev set
            dev_loss, dev_acc = get_dev_loss_and_acc(model, loss_fn, dev_dataloader, device)
            train_losses.append(train_loss / len(train_dataloader))
            dev_losses.append(dev_loss)
            train_accs.append(hits / total)
            dev_accs.append(dev_acc)
            tqdm.write("Epoch: {}, Train loss: {}, Train acc: {}, Dev loss: {}, Dev acc: {}".format(
                epoch, train_loss, hits / total, dev_loss, dev_acc))
            if dev_acc < prev_best:
                patience += 1
                if patience == 3:
                    # Reduce the learning rate by a factor of 2 if dev acc doesn't increase for 3 epochs
                    # Learning rate annealing
                    optim_state = optimizer.state_dict()
                    optim_state['param_groups'][0]['lr'] = optim_state['param_groups'][0]['lr'] / 2
                    optimizer.load_state_dict(optim_state)
                    tqdm.write('Dev accuracy did not increase, reducing the learning rate by 2 !!!')
                    patience = 0
            else:
                prev_best = dev_acc
                # Save the model
                torch.save(model.state_dict(), "models/model_{}.t7".format(args.config))

            # Acc vs time plot
        fig = plt.figure()
        plt.plot(range(1, args.max_epochs + 1), train_accs, color='b', label='train')
        plt.plot(range(1, args.max_epochs + 1), dev_accs, color='r', label='dev')
        plt.ylabel('accuracy')
        plt.xlabel('epochs')
        plt.legend()
        plt.xticks(np.arange(1, args.max_epochs + 1, step=4))
        fig.savefig('data/' + '{}_accuracy.png'.format(args.config))

    elif args.mode == 1:

        print("Preparing data...")
        test_examples = read_testset(args.test, args.max_seq_length)
        test_data_loader = get_data_loader(args, test_examples, tokenizer, bert_model, device)
        print("Preparing data...[OK]")

        model = BertForClassification(args, 2)
        model.to(device)

        if torch.cuda.is_available():
            model = model.cuda()
        # Load the model weights
        model.load_state_dict(torch.load("models/" + args.model_file, map_location=lambda storage, loc: storage))

        hits = 0
        total = 0
        model.eval()
        for input_features, input_labels in test_data_loader:
            logits = model(input_features)
            hits += torch.sum(torch.argmax(logits, dim=1) == input_labels).item()
            total += len(input_features)

        accuracy = hits / total
        print("Accuracy on the OOD test set: {}".format(accuracy))


if __name__ == "__main__":
    main()