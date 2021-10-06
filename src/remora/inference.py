import atexit
import os

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
from tqdm import tqdm

from remora import log, RemoraError, encoded_kmers
from remora.data_chunks import RemoraDataset, RemoraRead
from remora.util import (
    continue_from_checkpoint,
    Motif,
    validate_mod_bases,
    get_can_converter,
)

LOGGER = log.get_logger()


class resultsWriter:
    def __init__(self, output_path):
        self.sep = "\t"
        self.out_fp = open(output_path, "w")
        df = pd.DataFrame(
            columns=[
                "read_id",
                "read_pos",
                "label",
                "class_pred",
                "class_probs",
            ]
        )
        df.to_csv(self.out_fp, sep=self.sep, index=False)

    def write_results(self, output, read_data, labels):
        read_ids, read_pos = zip(*read_data)
        class_preds = output.argmax(dim=1)
        str_probs = [
            ",".join(map(str, r))
            for r in F.softmax(output, dim=1).detach().cpu().numpy()
        ]
        pd.DataFrame(
            {
                "read_id": read_ids,
                "read_pos": read_pos,
                "label": labels,
                "class_pred": class_preds,
                "class_probs": str_probs,
            }
        ).to_csv(self.out_fp, header=False, index=False, sep=self.sep)

    def close(self):
        self.out_fp.close()


def infer(
    input_msf,
    out_path,
    checkpoint_path,
    model_path,
    batch_size,
    device,
    focus_offset,
):
    LOGGER.info("Performing Remora inference")
    alphabet_info = input_msf.get_alphabet_information()
    alphabet, collapse_alphabet = (
        alphabet_info.alphabet,
        alphabet_info.collapse_alphabet,
    )

    if torch.cuda.is_available():
        torch.cuda.set_device(device)
    elif device is not None:
        LOGGER.warning(
            "Device option specified, but CUDA is not available from torch."
        )

    if focus_offset is not None:
        focus_offset = np.array([focus_offset])

    rw = resultsWriter(os.path.join(out_path, "results.tsv"))
    atexit.register(rw.close)

    LOGGER.info("Loading model")
    ckpt, model = continue_from_checkpoint(checkpoint_path, model_path)
    ckpt_attrs = "\n".join(
        f"  {k: >20} : {v}"
        for k, v in ckpt.items()
        if k not in ("state_dict", "opt")
    )
    LOGGER.debug(f"Loaded model attrs\n{ckpt_attrs}\n")
    if torch.cuda.is_available():
        model = model.cuda()
    model.eval()
    motif = Motif(*ckpt["motif"])
    if ckpt["base_pred"]:
        if alphabet != "ACGT":
            raise ValueError(
                "Base prediction is not compatible with modified base "
                "training data. It requires a canonical alphabet."
            )
        label_conv = get_can_converter(alphabet, collapse_alphabet)
    else:
        try:
            label_conv = validate_mod_bases(
                ckpt["mod_bases"], motif, alphabet, collapse_alphabet
            )
        except RemoraError:
            label_conv = None

    can_conv = get_can_converter(
        alphabet_info.alphabet, alphabet_info.collapse_alphabet
    )
    num_reads = len(input_msf.get_read_ids())
    bb, ab = ckpt["kmer_context_bases"]
    for read in tqdm(input_msf, smoothing=0, total=num_reads, unit="reads"):
        try:
            read = RemoraRead.from_taiyaki_read(read, can_conv, label_conv)
        except RemoraError:
            # TODO log these failed reads to track down errors
            continue
        if focus_offset is not None:
            motif_hits = focus_offset
        elif motif.any_context:
            motif_hits = np.arange(
                motif.focus_pos,
                read.can_seq.size - motif.num_bases_after_focus,
            )
        else:
            motif_hits = np.fromiter(read.iter_motif_hits(motif), int)
        chunks = list(
            read.iter_chunks(
                motif_hits,
                ckpt["chunk_context"],
                ckpt["kmer_context_bases"],
                ckpt["base_pred"],
            )
        )
        read_dataset = RemoraDataset.allocate_empty_chunks(
            num_chunks=len(motif_hits),
            chunk_context=ckpt["chunk_context"],
            max_seq_len=max(c.seq_len for c in chunks),
            kmer_context_bases=ckpt["kmer_context_bases"],
            base_pred=ckpt["base_pred"],
            mod_bases=ckpt["mod_bases"],
            motif=motif.to_tuple(),
            store_read_data=True,
            batch_size=batch_size,
            shuffle_on_iter=False,
            drop_last=False,
        )
        for chunk in chunks:
            read_dataset.add_chunk(chunk)
        read_dataset.set_nbatches()
        for (sigs, seqs, seq_maps, seq_lens), labels, read_data in read_dataset:
            enc_kmers = torch.from_numpy(
                encoded_kmers.compute_encoded_kmer_batch(
                    bb, ab, seqs, seq_maps, seq_lens
                )
            )
            if torch.cuda.is_available():
                sigs = sigs.cuda()
                enc_kmers = enc_kmers.cuda()
            output = model(sigs, enc_kmers).detach().cpu()
            rw.write_results(output, read_data, labels)


if __name__ == "__main__":
    NotImplementedError("This is a module.")
