import json
import time
import torch
import hydra
import random
import string
import logging
import os.path
from tqdm import tqdm
from omegaconf import DictConfig, OmegaConf, ListConfig

from funasr.register import tables
from funasr.utils.load_utils import load_bytes
from funasr.download.file import download_from_url
from funasr.download.download_from_hub import download_model
from funasr.utils.vad_utils import slice_padding_audio_samples
from funasr.train_utils.set_all_random_seed import set_all_random_seed
from funasr.train_utils.load_pretrained_model import load_pretrained_model
from funasr.utils.load_utils import load_audio_text_image_video, extract_fbank
from funasr.utils.timestamp_tools import timestamp_sentence
from funasr.models.campplus.utils import sv_chunk, postprocess, distribute_spk
from funasr.models.campplus.cluster_backend import ClusterBackend


def prepare_data_iterator(data_in, input_len=None, data_type=None, key=None):
    """
    
    :param input:
    :param input_len:
    :param data_type:
    :param frontend:
    :return:
    """
    data_list = []
    key_list = []
    filelist = [".scp", ".txt", ".json", ".jsonl"]
    
    chars = string.ascii_letters + string.digits
    if isinstance(data_in, str) and data_in.startswith('http'): # url
        data_in = download_from_url(data_in)
    if isinstance(data_in, str) and os.path.exists(data_in): # wav_path; filelist: wav.scp, file.jsonl;text.txt;
        _, file_extension = os.path.splitext(data_in)
        file_extension = file_extension.lower()
        if file_extension in filelist: #filelist: wav.scp, file.jsonl;text.txt;
            with open(data_in, encoding='utf-8') as fin:
                for line in fin:
                    key = "rand_key_" + ''.join(random.choice(chars) for _ in range(13))
                    if data_in.endswith(".jsonl"): #file.jsonl: json.dumps({"source": data})
                        lines = json.loads(line.strip())
                        data = lines["source"]
                        key = data["key"] if "key" in data else key
                    else: # filelist, wav.scp, text.txt: id \t data or data
                        lines = line.strip().split(maxsplit=1)
                        data = lines[1] if len(lines)>1 else lines[0]
                        key = lines[0] if len(lines)>1 else key
                    
                    data_list.append(data)
                    key_list.append(key)
        else:
            key = "rand_key_" + ''.join(random.choice(chars) for _ in range(13))
            data_list = [data_in]
            key_list = [key]
    elif isinstance(data_in, (list, tuple)):
        if data_type is not None and isinstance(data_type, (list, tuple)): # mutiple inputs
            data_list_tmp = []
            for data_in_i, data_type_i in zip(data_in, data_type):
                key_list, data_list_i = prepare_data_iterator(data_in=data_in_i, data_type=data_type_i)
                data_list_tmp.append(data_list_i)
            data_list = []
            for item in zip(*data_list_tmp):
                data_list.append(item)
        else:
            # [audio sample point, fbank, text]
            data_list = data_in
            key_list = ["rand_key_" + ''.join(random.choice(chars) for _ in range(13)) for _ in range(len(data_in))]
    else: # raw text; audio sample point, fbank; bytes
        if isinstance(data_in, bytes): # audio bytes
            data_in = load_bytes(data_in)
        if key is None:
            key = "rand_key_" + ''.join(random.choice(chars) for _ in range(13))
        data_list = [data_in]
        key_list = [key]
    
    return key_list, data_list

@hydra.main(config_name=None, version_base=None)
def main_hydra(cfg: DictConfig):
    def to_plain_list(cfg_item):
        if isinstance(cfg_item, ListConfig):
            return OmegaConf.to_container(cfg_item, resolve=True)
        elif isinstance(cfg_item, DictConfig):
            return {k: to_plain_list(v) for k, v in cfg_item.items()}
        else:
            return cfg_item
    
    kwargs = to_plain_list(cfg)
    log_level = getattr(logging, kwargs.get("log_level", "INFO").upper())

    logging.basicConfig(level=log_level)

    if kwargs.get("debug", False):
        import pdb; pdb.set_trace()
    model = AutoModel(**kwargs)
    res = model(input=kwargs["input"])
    print(res)

class AutoModel:
    
    def __init__(self, **kwargs):
        tables.print()
        
        model, kwargs = self.build_model(**kwargs)
        
        # if vad_model is not None, build vad model else None
        vad_model = kwargs.get("vad_model", None)
        vad_kwargs = kwargs.get("vad_model_revision", None)
        if vad_model is not None:
            logging.info("Building VAD model.")
            vad_kwargs = {"model": vad_model, "model_revision": vad_kwargs}
            vad_model, vad_kwargs = self.build_model(**vad_kwargs)

        # if punc_model is not None, build punc model else None
        punc_model = kwargs.get("punc_model", None)
        punc_kwargs = kwargs.get("punc_model_revision", None)
        if punc_model is not None:
            logging.info("Building punc model.")
            punc_kwargs = {"model": punc_model, "model_revision": punc_kwargs}
            punc_model, punc_kwargs = self.build_model(**punc_kwargs)

        # if spk_model is not None, build spk model else None
        spk_model = kwargs.get("spk_model", None)
        spk_kwargs = kwargs.get("spk_model_revision", None)
        if spk_model is not None:
            logging.info("Building SPK model.")
            spk_kwargs = {"model": spk_model, "model_revision": spk_kwargs}
            spk_model, spk_kwargs = self.build_model(**spk_kwargs)
            self.cb_model = ClusterBackend()
            spk_mode = kwargs.get("spk_mode", 'punc_segment')
            if spk_mode not in ["default", "vad_segment", "punc_segment"]:
                logging.error("spk_mode should be one of default, vad_segment and punc_segment.")
            self.spk_mode = spk_mode
            self.preset_spk_num = kwargs.get("preset_spk_num", None)
            if self.preset_spk_num:
                logging.warning("Using preset speaker number: {}".format(self.preset_spk_num))
            logging.warning("Many to print when using speaker model...")
            
        self.kwargs = kwargs
        self.model = model
        self.vad_model = vad_model
        self.vad_kwargs = vad_kwargs
        self.punc_model = punc_model
        self.punc_kwargs = punc_kwargs
        self.spk_model = spk_model
        self.spk_kwargs = spk_kwargs
        self.model_path = kwargs["model_path"]
  
        
    def build_model(self, **kwargs):
        assert "model" in kwargs
        if "model_conf" not in kwargs:
            logging.info("download models from model hub: {}".format(kwargs.get("model_hub", "ms")))
            kwargs = download_model(**kwargs)
        
        set_all_random_seed(kwargs.get("seed", 0))
        
        device = kwargs.get("device", "cuda")
        if not torch.cuda.is_available() or kwargs.get("ngpu", 0):
            device = "cpu"
            # kwargs["batch_size"] = 1
        kwargs["device"] = device
        
        if kwargs.get("ncpu", None):
            torch.set_num_threads(kwargs.get("ncpu"))
        
        # build tokenizer
        tokenizer = kwargs.get("tokenizer", None)
        if tokenizer is not None:
            tokenizer_class = tables.tokenizer_classes.get(tokenizer.lower())
            tokenizer = tokenizer_class(**kwargs["tokenizer_conf"])
            kwargs["tokenizer"] = tokenizer
            kwargs["token_list"] = tokenizer.token_list
            vocab_size = len(tokenizer.token_list)
        else:
            vocab_size = -1
        
        # build frontend
        frontend = kwargs.get("frontend", None)
        if frontend is not None:
            frontend_class = tables.frontend_classes.get(frontend.lower())
            frontend = frontend_class(**kwargs["frontend_conf"])
            kwargs["frontend"] = frontend
            kwargs["input_size"] = frontend.output_size()
        
        # build model
        model_class = tables.model_classes.get(kwargs["model"].lower())
        model = model_class(**kwargs, **kwargs["model_conf"], vocab_size=vocab_size)
        model.eval()
        model.to(device)
        
        # init_param
        init_param = kwargs.get("init_param", None)
        if init_param is not None:
            logging.info(f"Loading pretrained params from {init_param}")
            load_pretrained_model(
                model=model,
                init_param=init_param,
                ignore_init_mismatch=kwargs.get("ignore_init_mismatch", False),
                oss_bucket=kwargs.get("oss_bucket", None),
            )
        
        return model, kwargs
    
    def __call__(self, input, input_len=None, **cfg):
        if self.vad_model is None:
            return self.generate(input, input_len=input_len, **cfg)
            
        else:
            return self.generate_with_vad(input, input_len=input_len, **cfg)
        
    def generate(self, input, input_len=None, model=None, kwargs=None, key=None, **cfg):
        kwargs = self.kwargs if kwargs is None else kwargs
        kwargs.update(cfg)
        model = self.model if model is None else model

        batch_size = kwargs.get("batch_size", 1)
        # if kwargs.get("device", "cpu") == "cpu":
        #     batch_size = 1
        
        key_list, data_list = prepare_data_iterator(input, input_len=input_len, data_type=kwargs.get("data_type", None), key=key)
        
        speed_stats = {}
        asr_result_list = []
        num_samples = len(data_list)
        pbar = tqdm(colour="blue", total=num_samples+1, dynamic_ncols=True)
        time_speech_total = 0.0
        time_escape_total = 0.0
        for beg_idx in range(0, num_samples, batch_size):
            end_idx = min(num_samples, beg_idx + batch_size)
            data_batch = data_list[beg_idx:end_idx]
            key_batch = key_list[beg_idx:end_idx]
            batch = {"data_in": data_batch, "key": key_batch}
            if (end_idx - beg_idx) == 1 and isinstance(data_batch[0], torch.Tensor): # fbank
                batch["data_in"] = data_batch[0]
                batch["data_lengths"] = input_len
        
            time1 = time.perf_counter()
            with torch.no_grad():
                results, meta_data = model.generate(**batch, **kwargs)
            time2 = time.perf_counter()
            
            asr_result_list.extend(results)
            pbar.update(1)
            
            # batch_data_time = time_per_frame_s * data_batch_i["speech_lengths"].sum().item()
            batch_data_time = meta_data.get("batch_data_time", -1)
            time_escape = time2 - time1
            speed_stats["load_data"] = meta_data.get("load_data", 0.0)
            speed_stats["extract_feat"] = meta_data.get("extract_feat", 0.0)
            speed_stats["forward"] = f"{time_escape:0.3f}"
            speed_stats["batch_size"] = f"{len(results)}"
            speed_stats["rtf"] = f"{(time_escape) / batch_data_time:0.3f}"
            description = (
                f"{speed_stats}, "
            )
            pbar.set_description(description)
            time_speech_total += batch_data_time
            time_escape_total += time_escape
            
        pbar.update(1)
        pbar.set_description(f"rtf_avg: {time_escape_total/time_speech_total:0.3f}")
        torch.cuda.empty_cache()
        return asr_result_list
    
    def generate_with_vad(self, input, input_len=None, **cfg):
        
        # step.1: compute the vad model
        model = self.vad_model
        kwargs = self.vad_kwargs
        kwargs.update(cfg)
        beg_vad = time.time()
        res = self.generate(input, input_len=input_len, model=model, kwargs=kwargs, **cfg)
        vad_res = res
        end_vad = time.time()
        print(f"time cost vad: {end_vad - beg_vad:0.3f}")


        # step.2 compute asr model
        model = self.model
        kwargs = self.kwargs
        kwargs.update(cfg)
        batch_size = int(kwargs.get("batch_size_s", 300))*1000
        batch_size_threshold_ms = int(kwargs.get("batch_size_threshold_s", 60))*1000
        kwargs["batch_size"] = batch_size
        
        key_list, data_list = prepare_data_iterator(input, input_len=input_len, data_type=kwargs.get("data_type", None))
        results_ret_list = []
        time_speech_total_all_samples = 0.0

        beg_total = time.time()
        pbar_total = tqdm(colour="red", total=len(res) + 1, dynamic_ncols=True)
        for i in range(len(res)):
            key = res[i]["key"]
            vadsegments = res[i]["value"]
            input_i = data_list[i]
            speech = load_audio_text_image_video(input_i, fs=kwargs["frontend"].fs, audio_fs=kwargs.get("fs", 16000))
            speech_lengths = len(speech)
            n = len(vadsegments)
            data_with_index = [(vadsegments[i], i) for i in range(n)]
            sorted_data = sorted(data_with_index, key=lambda x: x[0][1] - x[0][0])
            results_sorted = []
            
            if not len(sorted_data):
                logging.info("decoding, utt: {}, empty speech".format(key))
                continue
            

            # if kwargs["device"] == "cpu":
            #     batch_size = 0
            if len(sorted_data) > 0 and len(sorted_data[0]) > 0:
                batch_size = max(batch_size, sorted_data[0][0][1] - sorted_data[0][0][0])
            
            batch_size_ms_cum = 0
            beg_idx = 0
            beg_asr_total = time.time()
            time_speech_total_per_sample = speech_lengths/16000
            time_speech_total_all_samples += time_speech_total_per_sample

            for j, _ in enumerate(range(0, n)):
                batch_size_ms_cum += (sorted_data[j][0][1] - sorted_data[j][0][0])
                if j < n - 1 and (
                    batch_size_ms_cum + sorted_data[j + 1][0][1] - sorted_data[j + 1][0][0]) < batch_size and (
                    sorted_data[j + 1][0][1] - sorted_data[j + 1][0][0]) < batch_size_threshold_ms:
                    continue
                batch_size_ms_cum = 0
                end_idx = j + 1
                speech_j, speech_lengths_j = slice_padding_audio_samples(speech, speech_lengths, sorted_data[beg_idx:end_idx])       
                results = self.generate(speech_j, input_len=None, model=model, kwargs=kwargs, **cfg)
                if self.spk_model is not None:
                    all_segments = []
                    # compose vad segments: [[start_time_sec, end_time_sec, speech], [...]]
                    for _b in range(len(speech_j)):
                        vad_segments = [[sorted_data[beg_idx:end_idx][_b][0][0]/1000.0, \
                                        sorted_data[beg_idx:end_idx][_b][0][1]/1000.0, \
                                        speech_j[_b]]]
                        segments = sv_chunk(vad_segments)
                        all_segments.extend(segments)
                        speech_b = [i[2] for i in segments]
                        spk_res = self.generate(speech_b, input_len=None, model=self.spk_model, kwargs=kwargs, **cfg)
                        results[_b]['spk_embedding'] = spk_res[0]['spk_embedding']
                beg_idx = end_idx
                if len(results) < 1:
                    continue
                results_sorted.extend(results)


            pbar_total.update(1)
            end_asr_total = time.time()
            time_escape_total_per_sample = end_asr_total - beg_asr_total
            pbar_total.set_description(f"rtf_avg_per_sample: {time_escape_total_per_sample / time_speech_total_per_sample:0.3f}, "
                                 f"time_speech_total_per_sample: {time_speech_total_per_sample: 0.3f}, "
                                 f"time_escape_total_per_sample: {time_escape_total_per_sample:0.3f}")

            restored_data = [0] * n
            for j in range(n):
                index = sorted_data[j][1]
                restored_data[index] = results_sorted[j]
            result = {}
            
            # results combine for texts, timestamps, speaker embeddings and others
            # TODO: rewrite for clean code
            for j in range(n):
                for k, v in restored_data[j].items():
                    if k.startswith("timestamp"):
                        if k not in result:
                            result[k] = []
                        for t in restored_data[j][k]:
                            t[0] += vadsegments[j][0]
                            t[1] += vadsegments[j][0]
                        result[k].extend(restored_data[j][k])
                    elif k == 'spk_embedding':
                        if k not in result:
                            result[k] = restored_data[j][k]
                        else:
                            result[k] = torch.cat([result[k], restored_data[j][k]], dim=0)
                    elif k == 'text':
                        if k not in result:
                            result[k] = restored_data[j][k]
                        else:
                            result[k] += " " + restored_data[j][k]
                    else:
                        if k not in result:
                            result[k] = restored_data[j][k]
                        else:
                            result[k] += restored_data[j][k]
                            
            # step.3 compute punc model
            if self.punc_model is not None:
                self.punc_kwargs.update(cfg)
                punc_res = self.generate(result["text"], model=self.punc_model, kwargs=self.punc_kwargs, **cfg)
                result["text_with_punc"] = punc_res[0]["text"]
                     
            # speaker embedding cluster after resorted
            if self.spk_model is not None:
                all_segments = sorted(all_segments, key=lambda x: x[0])
                spk_embedding = result['spk_embedding']
                labels = self.cb_model(spk_embedding, oracle_num=self.preset_spk_num)
                del result['spk_embedding']
                sv_output = postprocess(all_segments, None, labels, spk_embedding.cpu())
                if self.spk_mode == 'vad_segment':
                    sentence_list = []
                    for res, vadsegment in zip(restored_data, vadsegments):
                        sentence_list.append({"start": vadsegment[0],\
                                                "end": vadsegment[1],
                                                "sentence": res['text'],
                                                "timestamp": res['timestamp']})
                else: # punc_segment
                    sentence_list = timestamp_sentence(punc_res[0]['punc_array'], \
                                                        result['timestamp'], \
                                                        result['text'])
                distribute_spk(sentence_list, sv_output)
                result['sentence_info'] = sentence_list
                    
            result["key"] = key
            results_ret_list.append(result)
            pbar_total.update(1)
            
        pbar_total.update(1)
        end_total = time.time()
        time_escape_total_all_samples = end_total - beg_total
        pbar_total.set_description(f"rtf_avg_all_samples: {time_escape_total_all_samples / time_speech_total_all_samples:0.3f}, "
                             f"time_speech_total_all_samples: {time_speech_total_all_samples: 0.3f}, "
                             f"time_escape_total_all_samples: {time_escape_total_all_samples:0.3f}")
        return results_ret_list


class AutoFrontend:
    def __init__(self, **kwargs):
        assert "model" in kwargs
        if "model_conf" not in kwargs:
            logging.info("download models from model hub: {}".format(kwargs.get("model_hub", "ms")))
            kwargs = download_model(**kwargs)
        
        # build frontend
        frontend = kwargs.get("frontend", None)
        if frontend is not None:
            frontend_class = tables.frontend_classes.get(frontend.lower())
            frontend = frontend_class(**kwargs["frontend_conf"])

        self.frontend = frontend
        if "frontend" in kwargs:
            del kwargs["frontend"]
        self.kwargs = kwargs

    
    def __call__(self, input, input_len=None, kwargs=None, **cfg):
        
        kwargs = self.kwargs if kwargs is None else kwargs
        kwargs.update(cfg)


        key_list, data_list = prepare_data_iterator(input, input_len=input_len)
        batch_size = kwargs.get("batch_size", 1)
        device = kwargs.get("device", "cpu")
        if device == "cpu":
            batch_size = 1
        
        meta_data = {}
        
        result_list = []
        num_samples = len(data_list)
        pbar = tqdm(colour="blue", total=num_samples + 1, dynamic_ncols=True)
        
        time0 = time.perf_counter()
        for beg_idx in range(0, num_samples, batch_size):
            end_idx = min(num_samples, beg_idx + batch_size)
            data_batch = data_list[beg_idx:end_idx]
            key_batch = key_list[beg_idx:end_idx]

            # extract fbank feats
            time1 = time.perf_counter()
            audio_sample_list = load_audio_text_image_video(data_batch, fs=self.frontend.fs, audio_fs=kwargs.get("fs", 16000))
            time2 = time.perf_counter()
            meta_data["load_data"] = f"{time2 - time1:0.3f}"
            speech, speech_lengths = extract_fbank(audio_sample_list, data_type=kwargs.get("data_type", "sound"),
                                                   frontend=self.frontend, **kwargs)
            time3 = time.perf_counter()
            meta_data["extract_feat"] = f"{time3 - time2:0.3f}"
            meta_data["batch_data_time"] = speech_lengths.sum().item() * self.frontend.frame_shift * self.frontend.lfr_n / 1000
            
            speech.to(device=device), speech_lengths.to(device=device)
            batch = {"input": speech, "input_len": speech_lengths, "key": key_batch}
            result_list.append(batch)
            
            pbar.update(1)
            description = (
                f"{meta_data}, "
            )
            pbar.set_description(description)
        
        time_end = time.perf_counter()
        pbar.set_description(f"time escaped total: {time_end - time0:0.3f}")
        
        return result_list


if __name__ == '__main__':
    main_hydra()