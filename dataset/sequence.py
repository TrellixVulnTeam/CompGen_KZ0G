import framework
from models.encoder_decoder import add_eos
from typing import Tuple, Dict, Any, Optional, Callable, List
import torch
import torch.nn.functional as F

import regex as re

class SequenceTestState:
    def __init__(self, batch_dim: int = 1):
        self.n_ok = 0
        self.n_total = 0
        self.batch_dim = batch_dim
        self.time_dim = 1 - self.batch_dim

    def is_index_tensor(self, net_out: torch.Tensor) -> bool:
        return net_out.dtype in [torch.long, torch.int, torch.int8, torch.int16]

    def convert_to_index(self, net_out: torch.Tensor):
        return net_out if self.is_index_tensor(net_out) else net_out.argmax(-1)

    def compare_direct(self, net_out: Tuple[torch.Tensor, Optional[torch.Tensor]], ref: torch.Tensor,
                       ref_len: torch.Tensor):
        scores, len = net_out
        out = self.convert_to_index(scores)

        if len is not None:
            # Dynamic-length output
            if out.shape[0] > ref.shape[0]:
                out = out[: ref.shape[0]]
            elif out.shape[0] < ref.shape[0]:
                ref = ref[: out.shape[0]]

            unused = torch.arange(0, out.shape[0], dtype=torch.long, device=ref.device).unsqueeze(self.batch_dim) >= \
                     ref_len.unsqueeze(self.time_dim)

            ok_mask = ((out == ref) | unused).all(self.time_dim) & (len == ref_len)
        else:
            # Allow fixed lenght output
            assert out.shape==ref.shape
            ok_mask = (out == ref).all(self.time_dim)

        return ok_mask

    def compare_output(self, net_out: Tuple[torch.Tensor, Optional[torch.Tensor]], data: Dict[str, torch.Tensor]):
        return self.compare_direct(net_out, data["out"], data["out_len"])

    def step(self, net_out: Tuple[torch.Tensor, Optional[torch.Tensor]], data: Dict[str, torch.Tensor]):
        ok_mask = self.compare_output(net_out, data)

        self.n_total += ok_mask.nelement()
        self.n_ok += ok_mask.long().sum().item()

    @property
    def accuracy(self):
        return self.n_ok / self.n_total

    def plot(self) -> Dict[str, Any]:
        return {"accuracy/total": self.accuracy}


class TextSequenceTestState(SequenceTestState):
    def __init__(self, input_to_text: Callable[[torch.Tensor], torch.Tensor],
                 output_to_text: Callable[[torch.Tensor], torch.Tensor], batch_dim: int = 1,
                 max_bad_samples: int = 100, min_prefix_match_len: int = 1, eos_id: int = -1,
                 dataset='none'):
        super().__init__(batch_dim)
        self.bad_sequences = []
        self.max_bad_samples = max_bad_samples
        self.in_to_text = input_to_text
        self.out_to_text = output_to_text
        self.n_prefix_ok = 0
        self.n_oracle_ok = 0
        self.oracle_available = False
        self.min_prefix_match_len = min_prefix_match_len
        self.eos_id = eos_id
        self.losses = []
        self.oks = []
        if 'CFQ' in dataset:
            self.dataset = 'CFQ'
            self.wheres = 0
            self.inv_wheres = 0
            self.triples = 0
            self.inv_triples = 0
        elif 'COGS' in dataset:
            self.dataset = 'COGS'
            
        else:
            self.dataset = 'none'
        self.permuted_acc = 0
        self.isomorphic_acc = 0
        self.flexible_acc = 0
        
    def set_eos_to_neginf(self, scores: torch.Tensor) -> torch.Tensor:
        id = self.eos_id if self.eos_id >= 0 else (scores.shape[-1] + self.eos_id)
        return scores.index_fill(-1, torch.tensor([id], device=scores.device), float("-inf"))

    def loss(self, net_out: torch.Tensor, data: Dict[str, torch.Tensor]) -> torch.Tensor:
        mask = torch.arange(net_out.shape[1-self.batch_dim], device=net_out.device).unsqueeze(1) <= \
               data["out_len"].unsqueeze(0)

        ref = add_eos(data["out"], data["out_len"], net_out.shape[-1] - 1)
        l = F.cross_entropy(net_out.flatten(end_dim=-2), ref.long().flatten(), reduction='none')
        l = l.reshape_as(ref) * mask
        nonbatchdims = tuple(i for i in range(l.ndim) if i!=self.batch_dim)
        l = l.sum(dim=nonbatchdims) / mask.sum(dim=nonbatchdims).float()
        return l

    def sample_to_text(self, net_out: Tuple[torch.Tensor, Optional[torch.Tensor]], data: Dict[str, torch.Tensor],
                       i: int) -> Tuple[str, str, str]:

        scores, out_len = net_out
        out = self.convert_to_index(scores)

        t_ref = self.out_to_text(data["out"].select(self.batch_dim, i)[: int(data["out_len"][i].item())].
                cpu().numpy().tolist())

        out_end = None if out_len is None else out_len[i].item()
        t_out = self.out_to_text(out.select(self.batch_dim, i)[:out_end].cpu().numpy().tolist())
        t_in = self.in_to_text(data["in"].select(self.batch_dim, i)[: int(data["in_len"][i].item())].cpu().numpy().
                               tolist())
        return t_in, t_ref, t_out

    def get_wheres(self, text):
        wheres = re.findall('WHERE { .*? }', text)
        if len(wheres) != 1:
            return -1, -1
        wheres = wheres[0]
        remaining = text.replace(wheres,'')
        splits = wheres[8:-2].split(' . ')
        return splits, remaining

    def step(self, net_out: Tuple[torch.Tensor, Optional[torch.Tensor]], data: Dict[str, torch.Tensor]):
        ok_mask = self.compare_output(net_out, data)
        scores, _ = net_out

        if not self.is_index_tensor(scores):
            self.oracle_available = True
            out_noeos = self.set_eos_to_neginf(scores).argmax(-1)
            oracle_ok = self.compare_direct((out_noeos, data["out_len"].clamp_(max=out_noeos.shape[1-self.batch_dim])),
                                             data["out"], data["out_len"])
            self.n_oracle_ok += oracle_ok.long().sum().item()

            self.losses.append(self.loss(net_out[0], data).cpu())

        prefix_len = data["out_len"] if net_out[1] is None else torch.minimum(data["out_len"], net_out[1])
        prefix_len = torch.minimum(prefix_len.clamp(min=self.min_prefix_match_len), data["out_len"])
        prefix_ok_mask = self.compare_direct((net_out[0], prefix_len), data["out"], prefix_len)

        for i in range(net_out[0].shape[1]):
            t_in, t_ref, t_out = self.sample_to_text(net_out, data, i)
            s = [t_in, t_ref, t_out]

            isomorphic = False; permuted = False;
            if self.dataset == 'CFQ':
                ref_wheres, ref_rem = self.get_wheres(t_ref)
                try:
                    out_wheres, out_rem = self.get_wheres(t_out)
                except:
                    out_wheres = -1; out_rem = ''
                if out_wheres == -1:
                    self.inv_wheres += 1
                else:
                    self.inv_triples += len([_ for _ in out_wheres if len(_.split(' ')) != 3])
                    self.triples += len(out_wheres)
                    if set(out_wheres) == set(ref_wheres) and out_rem == ref_rem:
                        permuted = True
                
                t_out_ = t_out.split(' ')
                t_ref_ = t_ref.split(' ')
                if len(t_out_) == len(t_ref_) and ref_rem == out_rem:
                    map1 = {}; map2 = {}; isomorphic = True
                    for w1,w2 in zip(t_ref_,t_out_):
                        if w1 != w2 and (w1[:2] != '?x' or w2[:2] != '?x'):
                            isomorphic = False; break
                        if (w1 in map1 and map1[w1] != w2) or (w2 in map2 and map2[w2] != w1):
                            isomorphic = False; break
                        map1[w1] = w2
                        map2[w2] = w1
    
            elif self.dataset == 'COGS':
                ...
            if isomorphic and t_ref != t_out:
                self.isomorphic_acc += 1
            if permuted and t_ref != t_out:
                self.permuted_acc += 1
            if isomorphic or permuted or t_ref == t_out:
                self.flexible_acc += 1
            else:
                self.bad_sequences.append(s)

        if False: #if len(self.bad_sequences) < self.max_bad_samples:
            t = torch.nonzero(~ok_mask).squeeze(-1)[:self.max_bad_samples - len(self.bad_sequences)]

            for i in t:
                t_in, t_ref, t_out = self.sample_to_text(net_out, data, i)
                s = [t_in, t_ref, t_out, str(prefix_ok_mask[i].item())]

                if self.oracle_available:
                    s.append(str(oracle_ok[i].item()))

                self.bad_sequences.append(s)

        self.oks.append(ok_mask.cpu())
        self.n_total += ok_mask.nelement()
        self.n_ok += ok_mask.long().sum().item()
        self.n_prefix_ok += prefix_ok_mask.long().sum().item()

    def get_sample_info(self) -> Tuple[List[float], List[bool]]:
        return torch.cat(self.losses, 0).numpy().tolist(), torch.cat(self.oks, 0).numpy().tolist()

    def plot(self) -> Dict[str, Any]:
        res = super().plot()
        res["mistake_examples"] = framework.visualize.plot.TextTable(["Input", "Reference", "Output"],
                                                                     self.bad_sequences)
        res["accuracy/prefix"] = self.n_prefix_ok / self.n_total
        res["accuracy/permuted"] = self.permuted_acc / self.n_total
        res["accuracy/isomorphic"] = self.isomorphic_acc / self.n_total
        res["accuracy/flexible"] = self.flexible_acc / self.n_total
        
        if self.oracle_available:
            res["accuracy/oracle"] = self.n_oracle_ok / self.n_total

        if self.losses:
            res["loss_histogram"] = framework.visualize.plot.Histogram(torch.cat(self.losses, 0))

        return res


class TypedTextSequenceTestState(TextSequenceTestState):
    def __init__(self, input_to_text: Callable[[torch.Tensor], torch.Tensor],
                 output_to_text: Callable[[torch.Tensor], torch.Tensor], type_names: List[str], batch_dim: int = 1,
                 max_bad_samples: int = 100, dataset='none'):

        super().__init__(input_to_text, output_to_text, batch_dim, max_bad_samples, dataset=dataset)
        self.type_names = type_names

        self.count_per_type = {}

    def step(self, net_out: Tuple[torch.Tensor, Optional[torch.Tensor]], data: Dict[str, torch.Tensor]):
        ok_mask = self.compare_output(net_out, data)
        scores, out_len = net_out
        out = self.convert_to_index(scores)

        if len(self.bad_sequences) < self.max_bad_samples:
            t = torch.nonzero(~ok_mask).squeeze(-1)[:self.max_bad_samples - len(self.bad_sequences)]

            for i in t:
                out_end = None if out_len is None else out_len[i].item()
                self.bad_sequences.append((
                    self.in_to_text(data["in"].select(self.batch_dim, i)[: int(data["in_len"][i].item())].
                                    cpu().numpy().tolist()),
                    self.out_to_text(data["out"].select(self.batch_dim, i)[: int(data["out_len"][i].item())].
                                     cpu().numpy().tolist()),
                    self.type_names[int(data["type"][i].item())],
                    self.out_to_text(out.select(self.batch_dim, i)[:out_end].cpu().numpy().tolist())
                ))

        for t in torch.unique(data["type"]).int().cpu().numpy().tolist():
            mask = data["type"] == t
            c = self.count_per_type.get(t)
            if c is None:
                self.count_per_type[t] = c = {"n_ok": 0, "n_total": 0}

            c["n_total"] += mask.float().sum().item()
            c["n_ok"] += ok_mask[mask].float().sum().item()

        self.n_total += ok_mask.nelement()
        self.n_ok += ok_mask.long().sum().item()

    def plot(self) -> Dict[str, Any]:
        res = super().plot()
        res["mistake_examples"] = framework.visualize.plot.TextTable(["Input", "Reference", "Type", "Output"],
                                                                     self.bad_sequences)

        for t, data in self.count_per_type.items():
            res[f"accuracy/{self.type_names[t]}"] = data["n_ok"] / data["n_total"]
        return res
