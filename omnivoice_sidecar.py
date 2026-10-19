#!/usr/bin/env python3
"""Lektor z VoiceStudio (OmniVoice) jako proces w tle LiveDub.

Uruchamiany Pythonem z VoiceStudio: model ładuje się raz, głos domyślny to „voicepack” — wzorcowe nagranie
(voice.wav + voice.txt) wygenerowane z opisu, więc każda kwestia brzmi tym samym lektorem.

Klonowanie głosów postaci (09.10): proces umie też
- policzyć wektor mówcy z nagrania (wespeaker z cache pyannote) — silnik grupuje nimi wypowiedzi w postacie,
- zapamiętać głos postaci z próbki (create_voice_clone_prompt) i czytać nim kwestie.

Protokół (JSON w liniach, jedna odpowiedź na linię):
  stdin  {"id", "text", "speed", "out", "voice"?}                    → {"id", "ok", "error"?}   (synteza; voice = klucz postaci)
         {"op": "voice", "id", "key", "wav", "text"}                 → {"id", "ok", "secs"?}    (zapamiętaj głos postaci)
         {"op": "embed", "id", "wav"}                                → {"id", "ok", "emb": [...]} (wektor mówcy, wav 16 kHz mono)
Pierwsza linia na stdout: {"ready": true, "rate": 24000} albo {"ready": false, "error": "..."}.
"""
import json
import sys
import wave

RATE = 24000
PROTOCOL_OUT = sys.stdout
# PyTorch na MPS kompiluje i trzyma bez limitu osobny graf GPU dla każdego kształtu wejścia: każda nowa długość kwestii
# to ~55 MB RAM na zawsze (pomiar 19.10: 30 kwestii → +2 GB). Długości dopełniamy do wielokrotności — kształtów jest
# kilkanaście na całą sesję, a powtórzony kształt nie płaci też za kompilację.
SHAPE_STEP = 32
DECODE_TAIL = 12  # ostatnie ramki dźwięku liczone bez dopełnienia (zasięg splotów dekodera to ~8 ramek)


def reply(payload):
    PROTOCOL_OUT.write(json.dumps(payload, ensure_ascii=False) + "\n")
    PROTOCOL_OUT.flush()


class Embedder:
    """Wektor mówcy: pyannote/wespeaker-voxceleb-resnet34-LM z lokalnego cache (bez sieci).

    Załadowanie na żądanie — synteza nie czeka na model mówców, który i tak jest potrzebny dopiero po kilku wypowiedziach.
    Dwie łatki tylko na czas ładowania: nowszy huggingface_hub nie zna `use_auth_token`, a nowszy PyTorch żąda
    weights_only dla oficjalnego checkpointu pyannote z własnego cache."""

    def __init__(self):
        self.inf = None
        self.failed = None

    def load(self):
        import glob
        import os

        import torch

        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        import huggingface_hub as hub

        original_download, original_load = hub.hf_hub_download, torch.load
        hub.hf_hub_download = lambda *a, use_auth_token=None, **k: original_download(*a, **k)
        torch.load = lambda *a, **k: original_load(*a, **{**k, "weights_only": False})
        try:
            from pyannote.audio import Inference, Model

            from huggingface_hub.constants import HF_HUB_CACHE

            paths = sorted(glob.glob(f"{HF_HUB_CACHE}/models--pyannote--wespeaker-voxceleb-resnet34-LM/snapshots/*/pytorch_model.bin"))
            if not paths:
                raise RuntimeError("brak wespeaker w cache")
            # zostaje na CPU: na MPS każda nowa długość nagrania kompiluje graf (+14 MB na wypowiedź, na zawsze),
            # a CPU liczy ten sam wektor 2× szybciej (pomiar 19.10: ~85 ms vs ~200 ms)
            self.inf = Inference(Model.from_pretrained(paths[0]), window="whole")
        finally:
            hub.hf_hub_download, torch.load = original_download, original_load

    def embed(self, wav_path):
        import numpy as np
        import soundfile as sf
        import torch

        if self.failed:
            raise RuntimeError(self.failed)
        if self.inf is None:
            try:
                self.load()
            except Exception as exc:
                self.failed = str(exc)[:160]
                raise
        audio, rate = sf.read(wav_path, dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        vector = self.inf({"waveform": torch.from_numpy(audio).unsqueeze(0), "sample_rate": rate})
        vector = np.asarray(vector, dtype="float32").reshape(-1)
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(vector).all() or norm < 1e-9:
            raise ValueError("nieprawidłowy wektor mówcy")
        return (vector / norm).tolist()


def decode_on_gpu(model, device):
    """Dekoder audio (DAC z tokenizera Higgs) na GPU. VoiceStudio trzyma cały tokenizer na CPU, bo jego enkoder nie
    działa na MPS — a dekodowanie długiej kwestii na CPU alokuje ~1 GB buforów, które malloc macOS zostawia w procesie
    (pomiar 19.10: 3,5 → 8 GB po 20 kwestiach). Na MPS ten sam dźwięk (różnica ~1e-5) powstaje 4× szybciej i bez
    przyrostu RAM. Enkoder (wzorce głosów postaci) zostaje na CPU, dlatego kwantyzator na GPU jest kopią."""
    import copy

    import torch
    from transformers.models.higgs_audio_v2_tokenizer.modeling_higgs_audio_v2_tokenizer import (
        HiggsAudioV2TokenizerDecoderOutput,
    )

    tok = model.audio_tokenizer
    if device != "mps" or tok.device.type != "cpu":
        return
    quantizer = copy.deepcopy(tok.quantizer).to(device)
    tok.fc2.to(device)
    tok.acoustic_decoder.to(device)

    def run(codes):
        with torch.inference_mode():
            quantized = quantizer.decode(codes.transpose(0, 1))
            return tok.acoustic_decoder(tok.fc2(quantized.transpose(1, 2)).transpose(1, 2))

    def padded(codes, pad, left=False):
        fill = (codes[..., :1] if left else codes[..., -1:]).expand(*codes.shape[:-1], pad)
        return torch.cat([fill, codes] if left else [codes, fill], dim=-1)

    def decode(audio_codes, return_dict=None):
        codes = audio_codes.to(device)
        frames, hop = codes.shape[-1], tok.config.hop_length
        if frames < 2 * DECODE_TAIL or frames % SHAPE_STEP == 0:  # krótkich kształtów jest najwyżej kilkanaście
            audio = run(codes)
        else:
            # dopełnienie zmienia dźwięk ~8 ramek przy brzegu: środek z dopełnionej całości, a koniec z okna
            # SHAPE_STEP ostatnich prawdziwych ramek (zawsze ten sam kształt)
            head = run(padded(codes, -frames % SHAPE_STEP))[..., :(frames - DECODE_TAIL) * hop]
            window = codes[..., -SHAPE_STEP:] if frames >= SHAPE_STEP else padded(codes, SHAPE_STEP - frames, left=True)
            audio = torch.cat([head, run(window)[..., -DECODE_TAIL * hop:]], dim=-1)
        return HiggsAudioV2TokenizerDecoderOutput(audio.cpu())

    tok.decode = decode


def bucket_shapes(model):
    """OmniVoice._generate_iterative, w którym GPU liczy wyłącznie model — na długości dopełnionej do SHAPE_STEP.

    Każdy nowy kształt dowolnej operacji na MPS to kompilacja grafu, który zostaje w pamięci na zawsze (pomiar 19.10:
    ~15 grafów i ~13 MB na kwestię nawet po dopełnieniu samego modelu — budowa wejścia, maski, wybór tokenów). Wejście
    składamy więc na CPU, dopełnienie jest zamaskowane jak w batchu OmniVoice (widzi tylko siebie, pozycje prawdziwych
    tokenów bez zmian), a wybór tokenów liczy się na CPU (kilka ms na krok; CPU niczego nie kompiluje)."""
    import math

    import torch
    from omnivoice.models.omnivoice import _combine_text, _get_time_steps, _gumbel_sample, _tokenize_with_nonverbal_tags

    codebooks, mask_id = model.config.num_audio_codebook, model.config.audio_mask_id

    def prepare(text, num_target, ref_text, ref_tokens, lang, instruct, denoise):
        """OmniVoice._prepare_inference_inputs na CPU: (tokeny [C, N], początek audio w sekwencji)."""
        style = "<|denoise|>" if denoise and ref_tokens is not None else ""
        style += f"<|lang_start|>{lang or 'None'}<|lang_end|><|instruct_start|>{instruct or 'None'}<|instruct_end|>"
        wrapped = f"<|text_start|>{_combine_text(ref_text=ref_text, text=text)}<|text_end|>"
        parts = [model.text_tokenizer(style, return_tensors="pt").input_ids.repeat(codebooks, 1),
                 _tokenize_with_nonverbal_tags(wrapped, model.text_tokenizer).repeat(codebooks, 1)]
        if ref_tokens is not None:
            parts.append(ref_tokens.cpu())
        parts.append(torch.full((codebooks, num_target), mask_id, dtype=torch.long))
        ids = torch.cat(parts, dim=1)
        return ids, ids.shape[1] - num_target - (ref_tokens.size(-1) if ref_tokens is not None else 0)

    def generate_iterative(task, gen_config):
        batch, target_lens = task.batch_size, task.target_lens
        prepared = [prepare(task.texts[i], target_lens[i], task.ref_texts[i], task.ref_audio_tokens[i], task.langs[i],
                            task.instructs[i], gen_config.denoise) for i in range(batch)]
        c_lens = [ids.shape[1] for ids, _start in prepared]
        length = math.ceil(max(c_lens) / SHAPE_STEP) * SHAPE_STEP
        input_ids = torch.full((2 * batch, codebooks, length), mask_id, dtype=torch.long)
        audio_mask = torch.zeros((2 * batch, length), dtype=torch.bool)
        attention = torch.zeros((2 * batch, 1, length, length), dtype=torch.bool)
        for i, (ids, audio_start) in enumerate(prepared):
            c_len, u_len = c_lens[i], target_lens[i]
            input_ids[i, :, :c_len] = ids  # z warunkiem (tekst, wzorzec głosu)
            audio_mask[i, audio_start:c_len] = True
            attention[i, :, :c_len, :c_len] = True
            input_ids[batch + i, :, :u_len] = ids[:, -u_len:]  # bez warunku: same tokeny kwestii
            audio_mask[batch + i, :u_len] = True
            attention[batch + i, :, :u_len, :u_len] = True
        diagonal = torch.arange(length)
        attention[:, 0, diagonal, diagonal] |= ~attention[:, 0].any(dim=-1)
        device = model.device
        audio_mask_gpu, attention_gpu = audio_mask.to(device), attention.to(device)

        tokens = torch.full((batch, codebooks, max(target_lens)), mask_id, dtype=torch.long)
        timesteps = _get_time_steps(t_start=0.0, t_end=1.0, num_step=gen_config.num_step + 1,
                                    t_shift=gen_config.t_shift).tolist()
        schedules = []
        for t_len in target_lens:
            total = rem = t_len * codebooks
            schedule = []
            for step in range(gen_config.num_step):
                num = rem if step == gen_config.num_step - 1 else min(
                    math.ceil(total * (timesteps[step + 1] - timesteps[step])), rem)
                schedule.append(int(num))
                rem -= int(num)
            schedules.append(schedule)
        layer_ids = torch.arange(codebooks).view(1, -1, 1)

        for step in range(gen_config.num_step):
            logits = model(input_ids=input_ids.to(device), audio_mask=audio_mask_gpu,
                           attention_mask=attention_gpu).logits.cpu()
            for i in range(batch):
                k = schedules[i][step]
                if k <= 0:
                    continue
                c_len, t_len = c_lens[i], target_lens[i]
                pred_tokens, scores = model._predict_tokens_with_scoring(
                    logits[i:i + 1, :, c_len - t_len:c_len].float(), logits[batch + i:batch + i + 1, :, :t_len].float(),
                    gen_config)
                scores = scores - layer_ids * gen_config.layer_penalty_factor
                if gen_config.position_temperature > 0.0:
                    scores = _gumbel_sample(scores, gen_config.position_temperature)
                sample = tokens[i:i + 1, :, :t_len]
                scores.masked_fill_(sample != mask_id, -float("inf"))
                _values, top = torch.topk(scores.flatten(), k)
                flat = sample.flatten()
                flat[top] = pred_tokens.flatten()[top]
                sample.copy_(flat.view_as(sample))
                input_ids[i:i + 1, :, c_len - t_len:c_len] = sample
                input_ids[batch + i:batch + i + 1, :, :t_len] = sample
        return [tokens[i, :, :target_lens[i]] for i in range(batch)]

    model._generate_iterative = generate_iterative


def main():
    sys.stdout = sys.stderr  # biblioteki mogą pisać postęp; stdout protokołu zawiera wyłącznie JSON
    root, pack, steps = sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 12
    try:
        sys.path.insert(0, root)
        import numpy as np
        import torch
        from omnivoice.models.omnivoice import OmniVoice

        device = "mps" if torch.backends.mps.is_available() else "cpu"
        model = OmniVoice.from_pretrained(
            "k2-fsa/OmniVoice", device_map=device, dtype=torch.float16 if device == "mps" else torch.float32
        )
        if device == "mps":
            try:  # inna wersja VoiceStudio bez tych wnętrzności: czytamy dalej, tylko bez oszczędzania pamięci
                decode_on_gpu(model, device)
                bucket_shapes(model)
            except Exception as exc:
                print(f"OmniVoice bez kubełków kształtów: {exc}", file=sys.stderr)
        with open(f"{pack}/voice.txt", encoding="utf-8") as handle:
            ref_text = handle.read().strip()
        prompt = model.create_voice_clone_prompt(f"{pack}/voice.wav", ref_text)
        model.generate(text="Lektor gotowy.", language="pl", voice_clone_prompt=prompt, num_step=steps)
    except Exception as exc:  # brak VoiceStudio / modelu / voicepacka — LiveDub zostaje przy Supertonic
        reply({"ready": False, "error": str(exc)[:200]})
        return
    reply({"ready": True, "rate": RATE, "device": device})
    voices = {}  # klucz postaci → voice_clone_prompt (próbka z dźwięku gry; tylko w pamięci tego procesu)
    embedder = Embedder()
    for raw in sys.stdin:
        try:
            job = json.loads(raw)
        except json.JSONDecodeError:
            continue
        op = job.get("op")
        try:
            if op == "embed":
                reply({"id": job.get("id"), "ok": True, "emb": embedder.embed(job["wav"])})
                continue
            if op == "forget":
                voices.pop(job.get("key"), None)
                reply({"id": job.get("id"), "ok": True})
                continue
            if op == "voice":
                import time

                started = time.monotonic()
                voices[job["key"]] = model.create_voice_clone_prompt(job["wav"], job["text"])
                while len(voices) > 16:
                    del voices[next(iter(voices))]
                reply({"id": job.get("id"), "ok": True, "secs": round(time.monotonic() - started, 2)})
                continue
            speed = max(0.7, min(1.3, float(job.get("speed") or 1.0)))
            requested_voice = job.get("voice")
            if requested_voice and requested_voice not in voices:
                reply({"id": job.get("id"), "ok": False, "voice": None, "error": f"brak aktywnej próbki mówcy {requested_voice}"})
                continue
            chosen = voices[requested_voice] if requested_voice else prompt
            wav = model.generate(
                text=job["text"], language="pl", voice_clone_prompt=chosen, num_step=steps, speed=speed
            )[0]
            audio = np.clip(wav.squeeze().float().cpu().numpy(), -1.0, 1.0)
            with wave.open(job["out"], "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(RATE)
                handle.writeframes((audio * 32767.0).astype("<i2").tobytes())
            reply({"id": job.get("id"), "ok": True, "voice": requested_voice or "default"})
            if device == "mps":  # już po odpowiedzi: bufory ze szczytu kwestii wracają do systemu
                torch.mps.empty_cache()
        except Exception as exc:
            reply({"id": job.get("id"), "ok": False, "error": str(exc)[:200]})


if __name__ == "__main__":
    main()
