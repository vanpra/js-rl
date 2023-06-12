from datetime import datetime
import pickle

import torch
from torch.utils.data import Dataset
from torch.utils.data import random_split
from transformers import get_linear_schedule_with_warmup
from transformers import TrainingArguments
from transformers import RobertaForMaskedLM
from transformers import RobertaConfig
from transformers import Trainer
import torch.nn.functional as F


print("Loading data...")
with open("ASTBERTa/vocab_data.pkl", "rb") as f:
    vocab_data = pickle.load(f)

with open("ASTBERTa/data.pkl", "rb") as f:
    data = pickle.load(f)

PAD_TOKEN = "<pad>"
CLS_TOKEN = "<s>"
SEP_TOKEN = "</s>"
MASK_TOKEN = "<mask>"
UNK_TOKEN = "<unk>"

special_tokens = [PAD_TOKEN, CLS_TOKEN, MASK_TOKEN, SEP_TOKEN, UNK_TOKEN]

token_to_id = vocab_data["token_to_id"]
vocab = vocab_data["vocab"]


class ASTFragDataset(Dataset[list[int]]):
    def __init__(self, data: list[list[int]]):
        self.data = data

    def __len__(self):
        return len(self.data)

    def __getitem__(self, index: int) -> list[int]:
        return self.data[index]


start = datetime.now()
save_folder_name = start.strftime("%Y-%m-%dT%H:%M:.%f")

MAX_SEQ_LEN = 512
MLM_PROB = 0.15
MODEL_SAVE_PATH = f"ASTBERTa/models/{save_folder_name}"


def seq_data_collator(batch: list[list[int]]) -> dict[str, torch.Tensor]:
    seqs: list[torch.Tensor] = []

    for x in batch:
        if torch.rand(1).item() < 0.75:
            random_start_idx = torch.randint(low=2, high=len(x), size=(1,)).item()
            seq = [token_to_id[CLS_TOKEN]] + x[
                random_start_idx : random_start_idx + MAX_SEQ_LEN - 1
            ]
        else:
            seq = x[:MAX_SEQ_LEN]

        assert len(seq) <= MAX_SEQ_LEN
        seqs.append(torch.tensor(seq))

    inputs = torch.nn.utils.rnn.pad_sequence(seqs, batch_first=True)

    labels = inputs.clone()

    special_token_mask = torch.zeros_like(labels).float()
    special_token_mask[(labels >= 0) & (labels <= len(special_tokens))] = 1.0
    special_token_mask = special_token_mask.bool()

    probability_matrix = torch.full(labels.shape, MLM_PROB)
    probability_matrix.masked_fill_(special_token_mask, value=0.0)
    masked_indices = torch.bernoulli(probability_matrix).bool()

    # 80% of the time, we replace masked input tokens with tokenizer.mask_token ([MASK])
    indices_replaced = (
        torch.bernoulli(torch.full(labels.shape, 0.8)).bool() & masked_indices
    )
    inputs[indices_replaced] = token_to_id[MASK_TOKEN]
    labels[~masked_indices] = -100

    # 10% of the time, we replace masked input tokens with random word
    indices_random = (
        torch.bernoulli(torch.full(labels.shape, 0.5)).bool()
        & masked_indices
        & ~indices_replaced
    )
    random_words = torch.randint(len(vocab), labels.shape, dtype=torch.long)
    inputs[indices_random] = random_words[indices_random]

    attention_mask = torch.ones_like(inputs, dtype=torch.float)
    attention_mask[inputs == token_to_id[PAD_TOKEN]] = 0.0

    # The rest of the time (10% of the time) we keep the masked input tokens unchanged
    return {
        "input_ids": inputs,
        "labels": labels,
        "attention_mask": attention_mask,
    }


class ASTBERTaTrainer(Trainer):
    def compute_loss(self, model, inputs, return_outputs=False):
        labels = inputs["labels"]

        # forward pass
        outputs = model(**inputs)
        logits = outputs.get("logits")

        loss = F.cross_entropy(logits.view(-1, vocab_size), labels.view(-1))
        return (loss, outputs) if return_outputs else loss


device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

vocab_size = len(vocab)  # size of vocabulary
intermediate_size = 3072  # embedding dimension
hidden_size = 768

num_hidden_layers = 6
num_attention_heads = 12
dropout = 0.1

batch_size = 32

dataset = ASTFragDataset(data)
train_split, val_split, test_split = random_split(dataset, [0.8, 0.1, 0.1])

config = RobertaConfig(
    vocab_size=vocab_size,
    hidden_size=hidden_size,
    num_hidden_layers=num_hidden_layers,
    num_attention_heads=num_attention_heads,
    intermediate_size=intermediate_size,
    hidden_dropout_prob=dropout,
    max_position_embeddings=MAX_SEQ_LEN + 2,
)
model = RobertaForMaskedLM(config)

optim = torch.optim.AdamW(
    model.parameters(),
    lr=6e-4,
    eps=1e-6,
    weight_decay=0.01,
    betas=(0.9, 0.98),
)
lr_scheduler = get_linear_schedule_with_warmup(
    optimizer=optim, num_warmup_steps=2400, num_training_steps=50000
)

trainer = ASTBERTaTrainer(
    model=model,
    args=TrainingArguments(
        output_dir=MODEL_SAVE_PATH,
        overwrite_output_dir=True,
        num_train_epochs=100,
        per_device_train_batch_size=batch_size,
        per_device_eval_batch_size=batch_size,
        warmup_steps=2400,
        weight_decay=0.01,
        logging_dir=MODEL_SAVE_PATH,
        logging_steps=100,
        save_steps=500,
        save_total_limit=2,
    ),
    data_collator=seq_data_collator,
    train_dataset=train_split,
    eval_dataset=val_split,
    optimizers=(optim, lr_scheduler),
)

print(
    f"The model has {sum(p.numel() for p in model.parameters() if p.requires_grad):,} trainable parameters"
)
print(model)

# train(model, train_loader, val_loader, optim, lr_scheduler, epochs=100)

trainer.train()