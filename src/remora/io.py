import os
from copy import copy
from pathlib import Path
from dataclasses import dataclass
from collections import defaultdict
from typing import Iterator, Optional

import pysam
import numpy as np
from tqdm import tqdm
import pod5_format as p5
from pysam import AlignedSegment
from pod5_format import CombinedReader

from remora import log
from remora.util import revcomp
from remora.constants import PA_TO_NORM_SCALING_FACTOR
from remora import data_chunks as DC, duplex_utils as DU, RemoraError

LOGGER = log.get_logger()

# Note sm and sd tags are not required, but highly recommended to pass
# basecaller scaling into remora
REQUIRED_TAGS = {"mv", "MD"}

_SIG_PROF_FN = os.getenv("REMORA_EXTRACT_SIGNAL_PROFILE_FILE")
_ALIGN_PROF_FN = os.getenv("REMORA_EXTRACT_ALIGN_PROFILE_FILE")


def parse_bed(bed_path):
    regs = defaultdict(set)
    with open(bed_path) as regs_fh:
        for line in regs_fh:
            fields = line.split()
            ctg, st, en = fields[:3]
            if len(fields) < 6 or fields[5] not in "+-":
                for strand in "+-":
                    regs[(ctg, strand)].update(range(int(st), int(en)))
            else:
                regs[(ctg, fields[5])].update(range(int(st), int(en)))
    return regs


def parse_mods_bed(bed_path):
    regs = defaultdict(dict)
    all_mods = set()
    with open(bed_path) as regs_fh:
        for line in regs_fh:
            fields = line.split()
            ctg, st, en, mod = fields[:4]
            all_mods.update(mod)
            if len(fields) < 6 or fields[5] not in "+-":
                for strand in "+-":
                    for pos in range(int(st), int(en)):
                        regs[(ctg, strand)][pos] = mod
            else:
                for pos in range(int(st), int(en)):
                    regs[(ctg, fields[5])][pos] = mod
    return regs, all_mods


def read_is_primary(read):
    """
    :param read: pysam.AlignedSegment
    :return:
    """
    return not (read.is_supplementary or read.is_secondary)


def index_bam(
    bam_fp, skip_non_primary, req_tags=REQUIRED_TAGS, careful=False
) -> (dict, int):
    bam_idx = {} if skip_non_primary else defaultdict(list)
    num_reads = 0
    # hid warnings for no index when using unmapped or unsorted files
    pysam_save = pysam.set_verbosity(0)
    with pysam.AlignmentFile(bam_fp, mode="rb", check_sq=False) as bam_fh:
        pbar = tqdm(
            smoothing=0,
            unit=" Reads",
            desc="Indexing BAM by read id",
        )
        while True:
            read_ptr = bam_fh.tell()
            try:
                read = next(bam_fh)
            except StopIteration:
                break
            tags = set(tg[0] for tg in read.tags)
            if len(req_tags.intersection(tags)) != len(req_tags):
                if careful:
                    raise RemoraError("missing tags")
                continue
            if skip_non_primary:
                if not read_is_primary(read) or read.query_name in bam_idx:
                    continue
                bam_idx[read.query_name] = [read_ptr]
            else:
                bam_idx[read.query_name].append(read_ptr)
            num_reads += 1
            pbar.update()
    pysam.set_verbosity(pysam_save)
    return dict(bam_idx), num_reads


@dataclass
class RefPos:
    ctg: str
    strand: str
    start: int


@dataclass
class Read:
    read_id: str
    signal: Optional[np.ndarray] = None
    seq: str = None
    stride: int = None
    num_trimmed: int = None
    mv_table: np.array = None
    query_to_signal: np.ndarray = None
    shift_dacs_to_pa: float = None
    scale_dacs_to_pa: float = None
    shift_pa_to_norm: float = None
    scale_pa_to_norm: float = None
    shift_dacs_to_norm: float = None
    scale_dacs_to_norm: float = None
    ref_seq: str = None
    ref_pos: RefPos = None
    cigar: list = None
    ref_to_signal: np.ndarray = None
    full_align: str = None

    @staticmethod
    def convert_signal_to_pA(
        signal: np.ndarray, *, scale_dacs_to_pa: float, offset_dacs_to_pa: float
    ):
        return scale_dacs_to_pa * (signal + offset_dacs_to_pa)

    @staticmethod
    def compute_pa_to_norm_scaling(
        pa_signal: np.ndarray, factor: float = PA_TO_NORM_SCALING_FACTOR
    ) -> (float, float):
        shift_pa_to_norm = np.median(pa_signal)
        scale_pa_to_norm = max(
            1.0, np.median(np.abs(pa_signal - shift_pa_to_norm)) * factor
        )
        return shift_pa_to_norm, scale_pa_to_norm

    def set_pa_to_norm_scaling(self, factor=PA_TO_NORM_SCALING_FACTOR):
        assert self.scale_dacs_to_pa is not None
        assert self.shift_dacs_to_norm is not None
        pa_signal = Read.convert_signal_to_pA(
            self.signal,
            scale_dacs_to_pa=self.scale_dacs_to_pa,
            offset_dacs_to_pa=self.shift_dacs_to_pa,
        )
        shift_pa_to_norm, scale_pa_to_norm = Read.compute_pa_to_norm_scaling(
            pa_signal, factor=factor
        )
        self.shift_pa_to_norm = shift_pa_to_norm
        self.scale_pa_to_norm = scale_pa_to_norm

    def with_duplex_alignment(
        self,
        duplex_read_alignment: AlignedSegment,
        duplex_orientation: bool,
    ):
        assert self.query_to_signal is not None, "requires query_to_signal"
        assert (
            duplex_read_alignment.query_sequence is not None
        ), "no duplex base call sequence?"
        assert (
            len(duplex_read_alignment.query_sequence) > 0
        ), "duplex base call sequence is empty string?"

        read = copy(self)

        duplex_read_sequence = (
            duplex_read_alignment.query_sequence
            if duplex_orientation
            else revcomp(duplex_read_alignment.query_sequence)
        )

        # we don't have a mapping of each base in the simplex sequence to each
        # base in the duplex sequence, so we infer it by alignment. Using the
        # simplex sequence as the query sequence is somewhat arbitrary, but it
        # makes the downstream coordinate mappings more convenient
        simplex_duplex_mapping = DU.map_simplex_to_duplex(
            simplex_seq=read.seq, duplex_seq=duplex_read_sequence
        )
        # duplex_read_to_signal is a mapping of each position in the duplex
        # sequence to a signal datum from the read
        query_to_signal = read.query_to_signal
        duplex_to_read_signal = DC.map_ref_to_signal(
            query_to_signal=query_to_signal,
            ref_to_query_knots=simplex_duplex_mapping.duplex_to_simplex_mapping,
        )
        read.seq = simplex_duplex_mapping.trimmed_duplex_seq
        read.query_to_signal = duplex_to_read_signal

        read.ref_seq = None
        read.ref_to_signal = None
        read.ref_pos = None
        return read, simplex_duplex_mapping.duplex_offset

    @staticmethod
    def _unpack_reference_alignment(
        alignment_record: AlignedSegment, query_to_signal: np.ndarray
    ):
        ref_seq = alignment_record.get_reference_sequence()
        reverse_mapped = alignment_record.is_reverse
        ref_seq = ref_seq.upper()
        if reverse_mapped:
            ref_seq = revcomp(ref_seq)

        cigar = (
            alignment_record.cigartuples[::-1]
            if reverse_mapped
            else alignment_record.cigartuples
        )
        ref_to_signal = DC.compute_ref_to_signal(
            query_to_signal=query_to_signal,
            cigar=cigar,
            query_seq=alignment_record.query_sequence,
            ref_seq=ref_seq,
        )
        assert ref_to_signal.shape[0] == len(ref_seq) + 1
        # remember, pysam reverse-complements the mapped query_sequence on
        # reverse mapped records
        seq = (
            revcomp(alignment_record.query_sequence)
            if reverse_mapped
            else alignment_record.query_sequence
        )
        strand = "-" if reverse_mapped else "+"
        ref_pos = RefPos(
            ctg=alignment_record.reference_name,
            strand=strand,
            start=alignment_record.reference_start,
        )
        return {
            "ref_seq": ref_seq,
            "seq": seq,
            "cigar": cigar,
            "ref_pos": ref_pos,
            "ref_to_signal": ref_to_signal,
        }

    @classmethod
    def from_pod5_and_alignment(
        cls, pod5_read_record: p5.ReadRecord, alignment_record: AlignedSegment
    ):
        try:
            alignment_record.get_tag("mv")
        except KeyError as e:
            raise RemoraError(
                "aligned segment must have move table ('mv') tag"
            ) from e

        try:
            num_trimmed = alignment_record.get_tag("ts")
            signal = pod5_read_record.signal[num_trimmed:]
        except KeyError:
            num_trimmed = 0
            signal = pod5_read_record.signal

        mv_tag_value = alignment_record.get_tag("mv")
        stride = mv_tag_value[0]
        mv_table = np.array(mv_tag_value[1:])

        query_to_signal = np.nonzero(mv_table)[0] * stride
        query_to_signal = np.r_[query_to_signal, signal.shape[0]]

        if mv_table.shape[0] != signal.shape[0] // stride:
            raise RemoraError("move table is discordant with signal")
        if query_to_signal.shape[0] - 1 != alignment_record.query_length:
            raise RemoraError(
                "move table is discordant with base called sequence"
            )

        try:
            shift_pa_to_norm = alignment_record.get_tag("sm")
            scale_pa_to_norm = alignment_record.get_tag("sd")
        except KeyError:
            LOGGER.debug(
                "calculating pA to norm scale and offset, no tags found"
            )
            (
                shift_pa_to_norm,
                scale_pa_to_norm,
            ) = Read.compute_pa_to_norm_scaling(pod5_read_record.signal_pa)

        shift_dacs_to_norm = (
            shift_pa_to_norm / pod5_read_record.calibration.scale
        ) - pod5_read_record.calibration.offset
        scale_dacs_to_norm = (
            scale_pa_to_norm / pod5_read_record.calibration.scale
        )

        if alignment_record.reference_name is not None:
            properties = Read._unpack_reference_alignment(
                alignment_record, query_to_signal=query_to_signal
            )
        else:
            assert (
                not alignment_record.is_reverse
            ), "unmapped reads cannot be reverse!"
            properties = {
                "ref_seq": None,
                "seq": alignment_record.query_sequence,  # makes this OK
                "cigar": None,
                "ref_pos": None,
                "ref_to_signal": None,
            }

        read = Read(
            read_id=str(pod5_read_record.read_id),
            signal=signal,
            stride=stride,
            num_trimmed=num_trimmed,
            mv_table=mv_table,
            query_to_signal=query_to_signal,
            shift_dacs_to_pa=pod5_read_record.calibration.offset,
            scale_dacs_to_pa=pod5_read_record.calibration.offset,
            shift_pa_to_norm=shift_pa_to_norm,
            scale_pa_to_norm=scale_pa_to_norm,
            shift_dacs_to_norm=shift_dacs_to_norm,
            scale_dacs_to_norm=scale_dacs_to_norm,
            full_align=alignment_record.to_dict(),
            **properties,
        )

        return read

    def into_remora_read(self, use_reference_anchor: bool) -> DC.RemoraRead:
        if use_reference_anchor:
            assert self.ref_to_signal is not None, "need to have ref-to-signal"
            trim_signal = self.signal[
                self.ref_to_signal[0] : self.ref_to_signal[-1]
            ]
            shift_ref_to_sig = self.ref_to_signal - self.ref_to_signal[0]
            remora_read = DC.RemoraRead(
                dacs=trim_signal,
                shift=self.shift_dacs_to_norm,
                scale=self.scale_dacs_to_norm,
                seq_to_sig_map=shift_ref_to_sig,
                str_seq=self.ref_seq,
                read_id=self.read_id,
            )
        else:
            trim_signal = self.signal[
                self.query_to_signal[0] : self.query_to_signal[-1]
            ]
            shift_query_to_sig = self.query_to_signal - self.query_to_signal[0]
            remora_read = DC.RemoraRead(
                dacs=trim_signal,
                shift=self.shift_dacs_to_norm,
                scale=self.scale_dacs_to_norm,
                seq_to_sig_map=shift_query_to_sig,
                str_seq=self.seq,
                read_id=self.read_id,
            )
        remora_read.check()
        return remora_read


@dataclass
class DuplexRead:
    duplex_read_id: str
    duplex_alignment: dict
    is_reverse_mapped: bool
    template_read: Read
    complement_read: Read
    template_ref_start: int
    complement_ref_start: int

    @classmethod
    def from_reads_and_alignment(
        cls,
        *,
        template_read: Read,
        complement_read: Read,
        duplex_alignment: AlignedSegment,
    ):
        is_reverse_mapped = duplex_alignment.is_reverse
        duplex_direction_read, reverse_complement_read = (
            (template_read, complement_read)
            if not is_reverse_mapped
            else (complement_read, template_read)
        )

        (
            template_read,
            template_ref_start,
        ) = duplex_direction_read.with_duplex_alignment(
            duplex_alignment,
            duplex_orientation=True,
        )
        (
            complement_read,
            complement_ref_start,
        ) = reverse_complement_read.with_duplex_alignment(
            duplex_alignment,
            duplex_orientation=False,
        )

        return DuplexRead(
            duplex_read_id=duplex_alignment.query_name,
            duplex_alignment=duplex_alignment.to_dict(),
            is_reverse_mapped=is_reverse_mapped,
            template_read=template_read,
            complement_read=complement_read,
            template_ref_start=template_ref_start,
            complement_ref_start=complement_ref_start,
        )

    @property
    def duplex_basecalled_sequence(self):
        # n.b. pysam reverse-complements the query sequence on reverse mappings
        # [https://pysam.readthedocs.io/en/latest/api.html#pysam.AlignedSegment.query_sequence]
        return self.duplex_alignment["seq"]


##########################
# Signal then alignments #
##########################


def iter_pod5_reads(
    pod5_fp: str, num_reads: int = None, read_ids: Iterator = None
):
    LOGGER.debug(f"Reading from POD5 at {pod5_fp}")
    with CombinedReader(Path(pod5_fp)) as pod5_fh:  # todo change to pod5_fh
        for i, read in enumerate(
            pod5_fh.reads(selection=read_ids, preload=["samples"])
        ):
            if (
                num_reads is not None and i >= num_reads
            ):  # should this return an error?
                LOGGER.debug(
                    f"Completed pod5 signal worker, reached {num_reads}."
                )
                return

            yield read
    LOGGER.debug("Completed pod5 signal worker")


def iter_signal(pod5_fp, num_reads=None, read_ids=None):
    for pod5_read in iter_pod5_reads(
        pod5_fp=pod5_fp, num_reads=num_reads, read_ids=read_ids
    ):
        read = Read(
            read_id=str(pod5_read.read_id),
            signal=pod5_read.signal,
            shift_dacs_to_pa=pod5_read.calibration.offset,
            scale_dacs_to_pa=pod5_read.calibration.scale,
        )

        yield read, None
    LOGGER.debug("Completed signal worker")


class DuplexPairsIter:
    def __init__(self, *, pairs_fp: str, pod5_fp: str, simplex_bam_fp: str):
        self.pairs = iter(DuplexPairsIter.parse_pairs(pairs_fp))
        self.pod5_fp = pod5_fp
        self.reader = CombinedReader(Path(pod5_fp))
        self.alignments_fp = simplex_bam_fp
        self.simplex_index = None
        self.simplex_alignments = None
        self.p5_reads = None

    @staticmethod
    def parse_pairs(pairs_fp):
        pairs = []
        with open(pairs_fp, "r") as fh:
            seen_header = False
            for line in fh:
                if not seen_header:
                    seen_header = True
                    continue
                template, complement = line.split()
                pairs.append((template, complement))
        return pairs

    def __iter__(self):
        self.simplex_index, _ = index_bam(
            self.alignments_fp,
            skip_non_primary=True,
            req_tags={"mv"},
            careful=False,
        )
        self.simplex_alignments = pysam.AlignmentFile(
            self.alignments_fp, "rb", check_sq=False
        )
        return self

    def _make_read(self, p5_read: p5.ReadRecord):
        alns = self.simplex_index[str(p5_read.read_id)]
        assert len(alns) == 1, (
            "should not have multiple BAM records for simplex reads, make "
            "sure the index only has primary alignments"
        )
        self.simplex_alignments.seek(alns[0])
        alignment_record = next(self.simplex_alignments)
        io_read = Read.from_pod5_and_alignment(
            pod5_read_record=p5_read, alignment_record=alignment_record
        )
        return io_read

    def __next__(self):
        for read_id_pair in self.pairs:
            pod5_reads = self.reader.reads(
                selection=read_id_pair, preload=["samples"]
            )
            pod5_reads_filtered = [
                read for read in pod5_reads if str(read.read_id) in read_id_pair
            ]
            if len(pod5_reads_filtered) < 2:
                LOGGER.debug(f"read pair {read_id_pair} was not found in pod5")
                continue
            io_reads = {
                str(p5_read.read_id): self._make_read(p5_read)
                for p5_read in pod5_reads_filtered
            }
            template_read_id, complement_read_id = read_id_pair
            return io_reads[template_read_id], io_reads[complement_read_id]

        self.reader.close()
        self.simplex_alignments.close()
        raise StopIteration


if _SIG_PROF_FN:
    _iter_signal_wrapper = iter_signal

    def iter_signal(*args, **kwargs):
        import cProfile

        sig_prof = cProfile.Profile()
        retval = sig_prof.runcall(_iter_signal_wrapper, *args, **kwargs)
        sig_prof.dump_stats(_SIG_PROF_FN)
        return retval


def prep_extract_alignments(bam_idx, bam_fp, req_tags=REQUIRED_TAGS):
    pysam_save = pysam.set_verbosity(0)
    bam_fh = pysam.AlignmentFile(bam_fp, mode="rb", check_sq=False)
    pysam.set_verbosity(pysam_save)
    return [bam_idx, bam_fh], {"req_tags": req_tags}


def parse_move_tag(mv_tag, sig_len, seq_len=None, check=True):
    stride = mv_tag[0]
    mv_table = np.array(mv_tag[1:])
    query_to_signal = np.nonzero(mv_table)[0] * stride
    query_to_signal = np.concatenate([query_to_signal, [sig_len]])
    if check and seq_len is not None and query_to_signal.size - 1 != seq_len:
        LOGGER.debug(
            f"Move table (num moves: {query_to_signal.size - 1}) discordant "
            f"with basecalls (seq len: {seq_len})"
        )
        raise RemoraError("Move table discordant with basecalls")
    if check and mv_table.size != sig_len // stride:
        LOGGER.debug(
            f"Move table (len: {mv_table.size}) discordant with "
            f"signal (sig len // stride: {sig_len // stride})"
        )
        raise RemoraError("Move table discordant with signal")
    return query_to_signal, mv_table, stride


def extract_align_read(
    io_read, bam_read, req_tags=REQUIRED_TAGS, parse_ref_align=True
):
    tags = dict(bam_read.tags)
    if len(req_tags.intersection(tags)) != len(req_tags):
        return None
    try:
        io_read.num_trimmed = tags["ts"]
        io_read.signal = io_read.signal[io_read.num_trimmed :]
    except KeyError:
        io_read.num_trimmed = 0

    try:
        query_to_signal, mv_table, stride = parse_move_tag(
            tags["mv"],
            sig_len=io_read.signal.size,
            seq_len=len(bam_read.query_sequence),
        )
    except KeyError:
        raise RemoraError("Missing move table tag")

    try:
        io_read.shift_pa_to_norm = tags["sm"]
        io_read.scale_pa_to_norm = tags["sd"]
    except KeyError:
        io_read.set_pa_to_norm_scaling()

    io_read.shift_dacs_to_norm = (
        io_read.shift_pa_to_norm / io_read.scale_dacs_to_pa
    ) - io_read.shift_dacs_to_pa
    io_read.scale_dacs_to_norm = (
        io_read.scale_pa_to_norm / io_read.scale_dacs_to_pa
    )

    align_read = copy(io_read)
    align_read.seq = bam_read.query_sequence
    align_read.stride = stride
    align_read.mv_table = mv_table
    align_read.query_to_signal = query_to_signal
    align_read.full_align = bam_read.to_dict()
    if not parse_ref_align:
        return align_read

    try:
        ref_seq = bam_read.get_reference_sequence().upper()
    except ValueError:
        ref_seq = None
    cigar = bam_read.cigartuples
    if bam_read.is_reverse:
        align_read.seq = revcomp(align_read.seq)
        ref_seq = revcomp(ref_seq)
        cigar = cigar[::-1]
    ref_pos = RefPos(
        ctg=bam_read.reference_name,
        strand="-" if bam_read.is_reverse else "+",
        start=bam_read.reference_start,
    )
    align_read.ref_seq = ref_seq
    align_read.ref_pos = ref_pos
    align_read.cigar = cigar
    return align_read


def extract_alignments(read_err, bam_idx, bam_fh, req_tags=REQUIRED_TAGS):
    io_read, err = read_err
    if io_read is None:
        return [read_err]
    if io_read.read_id not in bam_idx:
        return [tuple((None, "Read id not found in BAM file"))]
    read_alignments = []
    for read_bam_ptr in bam_idx[io_read.read_id]:
        # jump to bam read pointer
        bam_fh.seek(read_bam_ptr)
        bam_read = next(bam_fh)
        try:
            align_read = extract_align_read(
                io_read, bam_read, req_tags=req_tags
            )
            if align_read is None:
                continue
        except RemoraError as e:
            read_alignments.append(tuple((None, str(e))))
            continue
        read_alignments.append(tuple((align_read, None)))
    return read_alignments


if _ALIGN_PROF_FN:
    _extract_align_wrapper = extract_alignments

    def extract_alignments(*args, **kwargs):
        import cProfile

        align_prof = cProfile.Profile()
        retval = align_prof.runcall(_extract_align_wrapper, *args, **kwargs)
        align_prof.dump_stats(_ALIGN_PROF_FN)
        return retval


##########################
# Alignments then signal #
##########################


def iter_alignments(
    bam_fp, num_reads, skip_non_primary, req_tags=REQUIRED_TAGS
):
    pysam_save = pysam.set_verbosity(0)
    with pysam.AlignmentFile(bam_fp, mode="rb", check_sq=False) as bam_fh:
        for read_num, read in enumerate(bam_fh.fetch(until_eof=True)):
            if num_reads is not None and read_num > num_reads:
                return
            if skip_non_primary and not read_is_primary(read):
                continue
            tags = dict(read.tags)
            if len(req_tags.intersection(tags)) != len(req_tags):
                continue
            mv_tag = tags["mv"]
            stride = mv_tag[0]
            mv_table = np.array(mv_tag[1:])
            query_to_signal = np.nonzero(mv_table)[0] * stride
            if query_to_signal.size != len(read.query_sequence):
                yield None, "Move table discordant with basecalls"
            try:
                num_trimmed = tags["ts"]
            except KeyError:
                num_trimmed = 0
            ref_seq = read.get_reference_sequence().upper()
            cigar = read.cigartuples
            if read.is_reverse:
                ref_seq = revcomp(ref_seq)
                cigar = cigar[::-1]
            ref_pos = RefPos(
                ctg=read.reference_name,
                strand="-" if read.is_reverse else "+",
                start=read.reference_start,
            )
            yield (
                Read(
                    read.query_name,
                    seq=read.query_sequence,
                    stride=stride,
                    mv_table=mv_table,
                    query_to_signal=query_to_signal,
                    num_trimmed=num_trimmed,
                    shift_pa_to_norm=tags.get("sm"),
                    scale_pa_to_norm=tags.get("sd"),
                    ref_seq=ref_seq,
                    ref_pos=ref_pos,
                    cigar=cigar,
                    full_align=read.to_dict(),
                ),
                None,
            )
    pysam.set_verbosity(pysam_save)


def prep_extract_signal(pod5_fp):
    pod5_fp = CombinedReader(Path(pod5_fp))
    return [
        pod5_fp,
    ], {}


def extract_signal(read_err, pod5_fp):
    read, err = read_err
    if read is None:
        return [read_err]
    pod5_read = next(pod5_fp.reads([read.read_id]))
    read.signal = pod5_read.signal[read.num_trimmed :]
    read.query_to_signal = np.concatenate(
        [read.query_to_signal, [read.signal.size]]
    )
    if read.mv_table.size != read.signal.size // read.stride:
        return [tuple((None, "Move table discordant with signal"))]
    read.shift_dacs_to_pa = pod5_read.calibration.offset
    read.scale_dacs_to_pa = pod5_read.calibration.scale
    if read.shift_pa_to_norm is None or read.scale_pa_to_norm is None:
        read.set_pa_to_norm_scaling()
    read.shift_dacs_to_norm = (
        read.shift_pa_to_norm / read.scale_dacs_to_pa
    ) - read.shift_dacs_to_pa
    read.scale_dacs_to_norm = read.scale_pa_to_norm / read.scale_dacs_to_pa
    return [tuple((read, None))]
