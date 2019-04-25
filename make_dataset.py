# external libraries
import os
import tqdm
import json
import zipfile
import tarfile
import random
import urllib.request

# internal utilities
import config
from utils import tokenizer, clean_text, word_tokenize, sent_tokenize, convert_idx

# URL to download SQuAD dataset 2.0
squad_url = "https://rajpurkar.github.io/SQuAD-explorer/dataset"


def maybe_download_squad(url, filename, out_dir):
    # path for local file.
    save_path = os.path.join(out_dir, filename)

    # check if the file already exists
    if not os.path.exists(save_path):
        # check if the output directory exists, otherwise create it.
        if not os.path.exists(out_dir):
            os.makedirs(out_dir)

        print("Downloading", filename, "...")

        # download the dataset
        url = os.path.join(url, filename)
        file_path, _ = urllib.request.urlretrieve(url=url, filename=save_path)

    print("File downloaded successfully!")

    if filename.endswith(".zip"):
        # unpack the zip-file.
        print("Extracting ZIP file...")
        zipfile.ZipFile(file=filename, mode="r").extractall(out_dir)
        print("File extracted successfully!")
    elif filename.endswith((".tar.gz", ".tgz")):
        # unpack the tar-ball.
        print("Extracting TAR file...")
        tarfile.open(name=filename, mode="r:gz").extractall(out_dir)
        print("File extracted successfully!")


class SquadPreprocessor:
    def __init__(self, data_dir, train_filename, dev_filename, tokenizer):
        self.data_dir = data_dir
        self.train_filename = train_filename
        self.dev_filename = dev_filename
        self.data = None
        self.tokenizer = tokenizer

    def load_data(self, filename="train-v2.0.json"):
        filepath = os.path.join(self.data_dir, filename)
        with open(filepath) as f:
            self.data = json.load(f)

    def split_data(self, filename):
        self.load_data(filename)
        sub_dir = filename.split('-')[0]

        # create a subdirectory for Train and Dev data
        if not os.path.exists(os.path.join(self.data_dir, sub_dir)):
            os.makedirs(os.path.join(self.data_dir, sub_dir))

        with open(os.path.join(self.data_dir, sub_dir, sub_dir + '.sentence'), 'w', encoding="utf-8") as sentence_file,\
             open(os.path.join(self.data_dir, sub_dir, sub_dir + '.question'), 'w', encoding="utf-8") as question_file,\
             open(os.path.join(self.data_dir, sub_dir, sub_dir + '.answer'), 'w', encoding="utf-8") as answer_file:

            # loop over the data
            for article_id in tqdm.tqdm(range(len(self.data['data']))):
                list_paragraphs = self.data['data'][article_id]['paragraphs']
                # loop over the paragraphs
                for paragraph in list_paragraphs:
                    context = paragraph['context']
                    context = clean_text(context)
                    context_tokens = word_tokenize(context)
                    context_sentences = sent_tokenize(context)
                    spans = convert_idx(context, context_tokens)
                    num_tokens = 0
                    sent_starts = []
                    for sentence in context_sentences:
                        first_sentence_span = spans[num_tokens][0]
                        num_tokens += len(sentence)
                        sent_starts.append(first_sentence_span)
                    qas = paragraph['qas']
                    # loop over Q/A
                    for qa in qas:
                        question = qa['question']
                        question = clean_text(question)
                        question_tokens = word_tokenize(question)
                        if sub_dir == "train":
                            # select only one ground truth, the top answer, if any answer
                            answer_ids = 1 if qa['answers'] else 0
                        else:
                            answer_ids = len(qa['answers'])
                        if answer_ids:
                            for answer_id in range(answer_ids):
                                answer = qa['answers'][answer_id]['text']
                                answer = clean_text(answer)
                                answer_tokens = word_tokenize(answer)
                                answer_start = qa['answers'][answer_id]['answer_start']
                                sentence_tokens = []
                                for idx, start in enumerate(sent_starts):
                                    if answer_start >= start:
                                        sentence_tokens = context_sentences[idx]
                                    else:
                                        break
                                if not sentence_tokens:
                                    print("Sentence cannot be found")
                                    raise Exception()

                            # write to file
                            sentence_file.write(' '.join([token for token in sentence_tokens]) + '\n')
                            question_file.write(' '.join([token for token in question_tokens]) + '\n')
                            answer_file.write(' '.join([token for token in answer_tokens]) + '\n')

    def preprocess(self):
        self.split_data(self.train_filename)
        self.split_data(self.dev_filename)


class NewsQAPreprocessor:
    def __init__(self, data_dir, filename, tokenizer):
        self.data_dir = data_dir
        self.filename = filename
        self.data = None
        self.tokenizer = tokenizer

    def load_data(self, filename="combined-newsqa-data-v1"):
        filepath = os.path.join(self.data_dir, filename)
        with open(filepath) as f:
            self.data = json.load(f)

    def split_data(self, filename):
        self.load_data(filename)

        envs = ["train", "dev"]
        for sub_dir in envs:
            # create a subdirectory for Train and Dev data
            if not os.path.exists(os.path.join(self.data_dir, sub_dir)):
                os.makedirs(os.path.join(self.data_dir, sub_dir))

            with open(os.path.join(self.data_dir, sub_dir, sub_dir + ".sentence"), "w", encoding="utf-8") as sentence_file,\
                 open(os.path.join(self.data_dir, sub_dir, sub_dir + ".question"), "w", encoding="utf-8") as question_file,\
                 open(os.path.join(self.data_dir, sub_dir, sub_dir + ".answer"), "w", encoding="utf-8") as answer_file:

                # loop over the data
                for article in tqdm.tqdm(self.data["data"]):
                    if not article["type"] == sub_dir:
                        continue
                    for question in article["questions"]:
                        if question.get("isQuestionBad") == 0 and question["consensus"].get("s"):
                            context = article["text"]
                            context_tokens = word_tokenize(context)
                            context_sentences = sent_tokenize(context)

                            spans = convert_idx(context, context_tokens)
                            num_tokens = 0
                            sent_starts = []
                            for sentence in context_sentences:
                                first_sentence_span = spans[num_tokens][0]
                                num_tokens += len(sentence)
                                sent_starts.append(first_sentence_span)

                            q = question["q"].strip()
                            if q[-1] != "?":
                                continue
                            answer_start = question["consensus"]["s"]
                            answer = context[question["consensus"]["s"]: question["consensus"]["e"]].strip(".| ").strip("\n")

                            for idx, start in enumerate(sent_starts):
                                if answer_start >= start:
                                    sentence_tokens = context_sentences[idx]
                                else:
                                    break
                            if not sentence_tokens:
                                print("Sentence cannot be found")
                                raise Exception()

                            sent = " ".join([token.strip("\n").strip() for token in sentence_tokens if token.strip("\n").strip()])
                            index = sent.find("( CNN ) -- ")
                            if index > -1:
                                sent = sent[index + len("( CNN ) -- "):]

                            # write to file
                            sentence_file.write(sent + "\n")
                            question_file.write(q + "\n")
                            answer_file.write(answer + "\n")

    def preprocess(self):
        self.split_data(self.filename)


def concatenate_data(filenames, out_filename):
    with open(out_filename, "w") as outfile:
        for fname in filenames:
            with open(fname) as infile:
                for line in infile:
                    outfile.write(line)
    with open(out_filename, "r") as f:
        lines = [(random.random(), line) for line in f]
    lines.sort()
    with open(out_filename, "w") as f:
        for _, line in lines:
            f.write(line)


if __name__ == "__main__":
    squad_train_filename = "train-v2.0.json"
    squad_dev_filename = "dev-v2.0.json"
    newsqa_filename = "combined-newsqa-data-v1.json"

    maybe_download_squad(squad_url, squad_train_filename, config.squad_data_dir)
    maybe_download_squad(squad_url, squad_dev_filename, config.squad_data_dir)

    p1 = NewsQAPreprocessor(config.newsqa_data_dir, newsqa_filename, tokenizer)
    p1.preprocess()

    p2 = SquadPreprocessor(config.squad_data_dir, squad_train_filename, squad_dev_filename, tokenizer)
    p2.preprocess()

    concatenate_data([os.path.join(config.squad_data_dir, "train", "train.sentence"),
                      os.path.join(config.newsqa_data_dir, "train", "train.sentence")],
                      os.path.join(config.out_dir, "train", "train.sentence"))

    concatenate_data([os.path.join(config.squad_data_dir, "train", "train.question"),
                      os.path.join(config.newsqa_data_dir, "train", "train.question")],
                      os.path.join(config.out_dir, "train", "train.question"))

    concatenate_data([os.path.join(config.squad_data_dir, "dev", "dev.sentence"),
                      os.path.join(config.newsqa_data_dir, "dev", "dev.sentence")],
                      os.path.join(config.out_dir, "dev", "dev.sentence"))

    concatenate_data([os.path.join(config.squad_data_dir, "dev", "dev.question"),
                      os.path.join(config.newsqa_data_dir, "dev", "dev.question")],
                      os.path.join(config.out_dir, "dev", "dev.question"))
