from io import open
from multiprocessing import Pool, cpu_count

try:
    import torchvision
    import torchvision.transforms as transforms

    torchvision_available = True
    from PIL import Image
except ImportError:
    torchvision_available = False

from scipy.stats import pearsonr, spearmanr
from sklearn.metrics import f1_score, matthews_corrcoef
from tqdm.auto import tqdm
import linecache

import torch
import torch.nn as nn
from torch.utils.data import Dataset


class InputExample(object):
    """A single training/test example for simple sequence classification."""

    def __init__(self, guid, text_a, text_b=None, label=None, features=None, x0=None, y0=None, x1=None, y1=None):
        """
        Constructs a InputExample.

        Args:
            guid: Unique id for the example.
            text_a: string. The untokenized text of the first sequence. For single
            sequence tasks, only this sequence must be specified.
            text_b: (Optional) string. The untokenized text of the second sequence.
            Only must be specified for sequence pair tasks.
            label: (Optional) string. The label of the example. This should be
            specified for train and dev examples, but not for test examples.
        """

        self.guid = guid
        self.text_a = text_a
        self.text_b = text_b
        self.label = label
        self.features = features
        if x0 is None:
            self.bboxes = None
        else:
            self.bboxes = [[a, b, c, d] for a, b, c, d in zip(x0, y0, x1, y1)]


class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self, input_ids, input_mask, segment_ids, label_id, feature_id, bboxes=None):
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.label_id = label_id
        self.feature_id = feature_id
        if bboxes:
            self.bboxes = bboxes


def convert_example_to_feature(
    example_row,
    pad_token=0,
    sequence_a_segment_id=0,
    sequence_b_segment_id=1,
    cls_token_segment_id=1,
    pad_token_segment_id=0,
    mask_padding_with_zero=True,
    sep_token_extra=False,
):
    (
        example,
        max_seq_length,
        tokenizer,
        output_mode,
        cls_token_at_end,
        cls_token,
        sep_token,
        cls_token_segment_id,
        pad_on_left,
        pad_token_segment_id,
        sep_token_extra,
        multi_label,
        stride,
        pad_token,
        add_prefix_space,
        pad_to_max_length,
    ) = example_row

    bboxes = []
    if example.bboxes:
        tokens_a = []
        for word, bbox in zip(example.text_a.split(), example.bboxes):
            word_tokens = tokenizer.tokenize(word)
            tokens_a.extend(word_tokens)
            bboxes.extend([bbox] * len(word_tokens))

        cls_token_box = [0, 0, 0, 0]
        sep_token_box = [1000, 1000, 1000, 1000]
        pad_token_box = [0, 0, 0, 0]

    else:
        # if add_prefix_space and not example.text_a.startswith(" "):
        #     tokens_a = tokenizer.tokenize(" " + example.text_a)
        # else:
        tokens_a = tokenizer.tokenize(example.text_a)

    tokens_b = None
    if example.text_b:
        # if add_prefix_space and not example.text_b.startswith(" "):
        #     tokens_b = tokenizer.tokenize(" " + example.text_b)
        # else:
        tokens_b = tokenizer.tokenize(example.text_b)
        # Modifies `tokens_a` and `tokens_b` in place so that the total
        # length is less than the specified length.
        # Account for [CLS], [SEP], [SEP] with "- 3". " -4" for RoBERTa.
        special_tokens_count = 4 if sep_token_extra else 3
        _truncate_seq_pair(tokens_a, tokens_b, max_seq_length - special_tokens_count)
    else:
        # Account for [CLS] and [SEP] with "- 2" and with "- 3" for RoBERTa.
        special_tokens_count = 3 if sep_token_extra else 2
        if len(tokens_a) > max_seq_length - special_tokens_count:
            tokens_a = tokens_a[: (max_seq_length - special_tokens_count)]
            if example.bboxes:
                bboxes = bboxes[: (max_seq_length - special_tokens_count)]

    # The convention in BERT is:
    # (a) For sequence pairs:
    #  tokens:   [CLS] is this jack ##son ##ville ? [SEP] no it is not . [SEP]
    #  type_ids:   0   0  0    0    0     0       0   0   1  1  1  1   1   1
    # (b) For single sequences:
    #  tokens:   [CLS] the dog is hairy . [SEP]
    #  type_ids:   0   0   0   0  0     0   0
    #
    # Where "type_ids" are used to indicate whether this is the first
    # sequence or the second sequence. The embedding vectors for `type=0` and
    # `type=1` were learned during pre-training and are added to the wordpiece
    # embedding vector (and position vector). This is not *strictly* necessary
    # since the [SEP] token unambiguously separates the sequences, but it makes
    # it easier for the model to learn the concept of sequences.
    #
    # For classification tasks, the first vector (corresponding to [CLS]) is
    # used as as the "sentence vector". Note that this only makes sense because
    # the entire model is fine-tuned.
    tokens = tokens_a + [sep_token]
    segment_ids = [sequence_a_segment_id] * len(tokens)

    if bboxes:
        bboxes += [sep_token_box]

    if tokens_b:
        if sep_token_extra:
            tokens += [sep_token]
            segment_ids += [sequence_b_segment_id]

        tokens += tokens_b + [sep_token]

        segment_ids += [sequence_b_segment_id] * (len(tokens_b) + 1)

    if cls_token_at_end:
        tokens = tokens + [cls_token]
        segment_ids = segment_ids + [cls_token_segment_id]
    else:
        tokens = [cls_token] + tokens
        segment_ids = [cls_token_segment_id] + segment_ids
        if bboxes:
            bboxes = [cls_token_box] + bboxes

    input_ids = tokenizer.convert_tokens_to_ids(tokens)

    # The mask has 1 for real tokens and 0 for padding tokens. Only real
    # tokens are attended to.
    input_mask = [1 if mask_padding_with_zero else 0] * len(input_ids)

    # Zero-pad up to the sequence length.
    if pad_to_max_length:
        padding_length = max_seq_length - len(input_ids)
        if pad_on_left:
            input_ids = ([pad_token] * padding_length) + input_ids
            input_mask = ([0 if mask_padding_with_zero else 1] * padding_length) + input_mask
            segment_ids = ([pad_token_segment_id] * padding_length) + segment_ids
        else:
            input_ids = input_ids + ([pad_token] * padding_length)
            input_mask = input_mask + ([0 if mask_padding_with_zero else 1] * padding_length)
            segment_ids = segment_ids + ([pad_token_segment_id] * padding_length)
            if bboxes:
                bboxes += [pad_token_box] * padding_length

        assert len(input_ids) == max_seq_length
        assert len(input_mask) == max_seq_length
        assert len(segment_ids) == max_seq_length
        if bboxes:
            assert len(bboxes) == max_seq_length
    # if output_mode == "classification":
    #     label_id = label_map[example.label]
    # elif output_mode == "regression":
    #     label_id = float(example.label)
    # else:
    #     raise KeyError(output_mode)

    # if output_mode == "regression":
    if bboxes:
        return InputFeatures(
            input_ids=input_ids, input_mask=input_mask, segment_ids=segment_ids, label_id=example.label, feature_id=example.features, bboxes=bboxes
        )
    else:
        return InputFeatures(
            input_ids=input_ids, input_mask=input_mask, segment_ids=segment_ids, label_id=example.label, feature_id=example.features
        )


def convert_examples_to_features(
    examples,
    max_seq_length,
    tokenizer,
    output_mode,
    cls_token_at_end=False,
    sep_token_extra=False,
    pad_on_left=False,
    cls_token="[CLS]",
    sep_token="[SEP]",
    pad_token=0,
    sequence_a_segment_id=0,
    sequence_b_segment_id=1,
    cls_token_segment_id=1,
    pad_token_segment_id=0,
    mask_padding_with_zero=True,
    process_count=cpu_count() - 2,
    multi_label=False,
    silent=False,
    use_multiprocessing=True,
    sliding_window=False,
    flatten=False,
    stride=None,
    add_prefix_space=False,
    pad_to_max_length=True,
    args=None,
):
    """ Loads a data file into a list of `InputBatch`s
        `cls_token_at_end` define the location of the CLS token:
            - False (Default, BERT/XLM pattern): [CLS] + A + [SEP] + B + [SEP]
            - True (XLNet/GPT pattern): A + [SEP] + B + [SEP] + [CLS]
        `cls_token_segment_id` define the segment id associated to the CLS token (0 for BERT, 2 for XLNet)
    """

    examples = [
        (
            example,
            max_seq_length,
            tokenizer,
            output_mode,
            cls_token_at_end,
            cls_token,
            sep_token,
            cls_token_segment_id,
            pad_on_left,
            pad_token_segment_id,
            sep_token_extra,
            multi_label,
            stride,
            pad_token,
            add_prefix_space,
            pad_to_max_length,
        )
        for example in examples
    ]

    if use_multiprocessing:
        if args.multiprocessing_chunksize == -1:
            chunksize = max(len(examples) // (args.process_count * 2), 500)
        else:
            chunksize = args.multiprocessing_chunksize
        if sliding_window:
            with Pool(process_count) as p:
                features = list(
                    tqdm(
                        p.imap(convert_example_to_feature_sliding_window, examples, chunksize=chunksize,),
                        total=len(examples),
                        disable=silent,
                    )
                )
            if flatten:
                features = [feature for feature_set in features for feature in feature_set]
        else:
            with Pool(process_count) as p:
                features = list(
                    tqdm(
                        p.imap(convert_example_to_feature, examples, chunksize=chunksize),
                        total=len(examples),
                        disable=silent,
                    )
                )
    else:
        if sliding_window:
            features = [
                convert_example_to_feature_sliding_window(example) for example in tqdm(examples, disable=silent)
            ]
            if flatten:
                features = [feature for feature_set in features for feature in feature_set]
        else:
            features = [convert_example_to_feature(example) for example in tqdm(examples, disable=silent)]

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

def convert_example_to_feature_sliding_window(
    example_row,
    pad_token=0,
    sequence_a_segment_id=0,
    sequence_b_segment_id=1,
    cls_token_segment_id=1,
    pad_token_segment_id=0,
    mask_padding_with_zero=True,
    sep_token_extra=False,
):
    (
        example,
        max_seq_length,
        tokenizer,
        output_mode,
        cls_token_at_end,
        cls_token,
        sep_token,
        cls_token_segment_id,
        pad_on_left,
        pad_token_segment_id,
        sep_token_extra,
        multi_label,
        stride,
    ) = example_row

    if stride < 1:
        stride = int(max_seq_length * stride)

    bucket_size = max_seq_length - (3 if sep_token_extra else 2)
    token_sets = []

    tokens_a = tokenizer.tokenize(example.text_a)

    if len(tokens_a) > bucket_size:
        token_sets = [tokens_a[i : i + bucket_size] for i in range(0, len(tokens_a), stride)]
    else:
        token_sets.append(tokens_a)

    if example.text_b:
        raise ValueError("Sequence pair tasks not implemented for sliding window tokenization.")

    # The convention in BERT is:
    # (a) For sequence pairs:
    #  tokens:   [CLS] is this jack ##son ##ville ? [SEP] no it is not . [SEP]
    #  type_ids:   0   0  0    0    0     0       0   0   1  1  1  1   1   1
    # (b) For single sequences:
    #  tokens:   [CLS] the dog is hairy . [SEP]
    #  type_ids:   0   0   0   0  0     0   0
    #
    # Where "type_ids" are used to indicate whether this is the first
    # sequence or the second sequence. The embedding vectors for `type=0` and
    # `type=1` were learned during pre-training and are added to the wordpiece
    # embedding vector (and position vector). This is not *strictly* necessary
    # since the [SEP] token unambiguously separates the sequences, but it makes
    # it easier for the model to learn the concept of sequences.
    #
    # For classification tasks, the first vector (corresponding to [CLS]) is
    # used as as the "sentence vector". Note that this only makes sense because
    # the entire model is fine-tuned.

    input_features = []
    for tokens_a in token_sets:
        tokens = tokens_a + [sep_token]
        segment_ids = [sequence_a_segment_id] * len(tokens)

        if cls_token_at_end:
            tokens = tokens + [cls_token]
            segment_ids = segment_ids + [cls_token_segment_id]
        else:
            tokens = [cls_token] + tokens
            segment_ids = [cls_token_segment_id] + segment_ids

        input_ids = tokenizer.convert_tokens_to_ids(tokens)

        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.
        input_mask = [1 if mask_padding_with_zero else 0] * len(input_ids)

        # Zero-pad up to the sequence length.
        padding_length = max_seq_length - len(input_ids)
        if pad_on_left:
            input_ids = ([pad_token] * padding_length) + input_ids
            input_mask = ([0 if mask_padding_with_zero else 1] * padding_length) + input_mask
            segment_ids = ([pad_token_segment_id] * padding_length) + segment_ids
        else:
            input_ids = input_ids + ([pad_token] * padding_length)
            input_mask = input_mask + ([0 if mask_padding_with_zero else 1] * padding_length)
            segment_ids = segment_ids + ([pad_token_segment_id] * padding_length)

        assert len(input_ids) == max_seq_length
        assert len(input_mask) == max_seq_length
        assert len(segment_ids) == max_seq_length

        # if output_mode == "classification":
        #     label_id = label_map[example.label]
        # elif output_mode == "regression":
        #     label_id = float(example.label)
        # else:
        #     raise KeyError(output_mode)

        input_features.append(
            InputFeatures(input_ids=input_ids, input_mask=input_mask, segment_ids=segment_ids, label_id=example.label,)
        )

    return input_features


class LazyClassificationDataset(Dataset):
    def __init__(self, data_file, tokenizer, args):
        self.data_file = data_file
        self.start_row = args.lazy_loading_start_line
        self.num_entries = self._get_n_lines(self.data_file, self.start_row)
        self.tokenizer = tokenizer
        self.args = args
        self.delimiter = args.lazy_delimiter
        if args.lazy_text_a_column is not None and args.lazy_text_b_column is not None:
            self.text_a_column = args.lazy_text_a_column
            self.text_b_column = args.lazy_text_b_column
            self.text_column = None
        else:
            self.text_column = args.lazy_text_column
            self.text_a_column = None
            self.text_b_column = None
        self.labels_column = args.lazy_labels_column

    @staticmethod
    def _get_n_lines(data_file, start_row):
        with open(data_file, encoding="utf-8") as f:
            for line_idx, _ in enumerate(f, 1):
                pass

        return line_idx - start_row

    def __getitem__(self, idx):
        line = linecache.getline(self.data_file, idx + 1 + self.start_row).rstrip("\n").split(self.delimiter)

        if not self.text_a_column and not self.text_b_column:
            text = line[self.text_column]
            label = line[self.labels_column]

            # If labels_map is defined, then labels need to be replaced with ints
            if self.args.labels_map:
                label = self.args.labels_map[label]
            if self.args.regression:
                label = torch.tensor(float(label), dtype=torch.float)
            else:
                label = torch.tensor(int(label), dtype=torch.long)

            return (
                self.tokenizer.encode_plus(
                    text,
                    max_length=self.args.max_seq_length,
                    pad_to_max_length=self.args.max_seq_length,
                    return_tensors="pt",
                ),
                label,
            )
        else:
            text_a = line[self.text_a_column]
            text_b = line[self.text_b_column]
            label = line[self.labels_column]
            if self.args.regression:
                label = torch.tensor(float(label), dtype=torch.float)
            else:
                label = torch.tensor(int(label), dtype=torch.long)

            return (
                self.tokenizer.encode_plus(
                    text_a,
                    text_pair=text_b,
                    max_length=self.args.max_seq_length,
                    pad_to_max_length=self.args.max_seq_length,
                    return_tensors="pt",
                ),
                label,
            )

    def __len__(self):
        return self.num_entries
