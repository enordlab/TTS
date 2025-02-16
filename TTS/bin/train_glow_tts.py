#!/usr/bin/env python3
"""Train Glow TTS model."""

import os
import sys
import time
import traceback
from random import randrange

import torch

# DISTRIBUTED
from torch.nn.parallel import DistributedDataParallel as DDP_th
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler

from TTS.tts.datasets.preprocess import load_meta_data
from TTS.tts.datasets.TTSDataset import MyDataset
from TTS.tts.layers.losses import GlowTTSLoss
from TTS.tts.utils.generic_utils import setup_model
from TTS.tts.utils.io import save_best_model, save_checkpoint
from TTS.tts.utils.measures import alignment_diagonal_score
from TTS.tts.utils.speakers import parse_speakers
from TTS.tts.utils.synthesis import synthesis
from TTS.tts.utils.text.symbols import make_symbols, phonemes, symbols
from TTS.tts.utils.visual import plot_alignment, plot_spectrogram
from TTS.utils.arguments import init_training
from TTS.utils.audio import AudioProcessor
from TTS.utils.distribute import init_distributed, reduce_tensor
from TTS.utils.generic_utils import KeepAverage, count_parameters, remove_experiment_folder, set_init_dict
from TTS.utils.radam import RAdam
from TTS.utils.training import NoamLR, setup_torch_training_env

use_cuda, num_gpus = setup_torch_training_env(True, False)


def setup_loader(ap, r, is_val=False, verbose=False):
    if is_val and not config.run_eval:
        loader = None
    else:
        dataset = MyDataset(
            r,
            config.text_cleaner,
            compute_linear_spec=False,
            meta_data=meta_data_eval if is_val else meta_data_train,
            ap=ap,
            tp=config.characters,
            add_blank=config["add_blank"],
            batch_group_size=0 if is_val else config.batch_group_size * config.batch_size,
            min_seq_len=config.min_seq_len,
            max_seq_len=config.max_seq_len,
            phoneme_cache_path=config.phoneme_cache_path,
            use_phonemes=config.use_phonemes,
            phoneme_language=config.phoneme_language,
            enable_eos_bos=config.enable_eos_bos_chars,
            use_noise_augment=not is_val,
            verbose=verbose,
            speaker_mapping=speaker_mapping
            if config.use_speaker_embedding and config.use_external_speaker_embedding_file
            else None,
        )

        if config.use_phonemes and config.compute_input_seq_cache:
            # precompute phonemes to have a better estimate of sequence lengths.
            dataset.compute_input_seq(config.num_loader_workers)
        dataset.sort_items()

        sampler = DistributedSampler(dataset) if num_gpus > 1 else None
        loader = DataLoader(
            dataset,
            batch_size=config.eval_batch_size if is_val else config.batch_size,
            shuffle=False,
            collate_fn=dataset.collate_fn,
            drop_last=False,
            sampler=sampler,
            num_workers=config.num_val_loader_workers if is_val else config.num_loader_workers,
            pin_memory=False,
        )
    return loader


def format_data(data):
    # setup input data
    text_input = data[0]
    text_lengths = data[1]
    speaker_names = data[2]
    mel_input = data[4].permute(0, 2, 1)  # B x D x T
    mel_lengths = data[5]
    item_idx = data[7]
    attn_mask = data[9]
    avg_text_length = torch.mean(text_lengths.float())
    avg_spec_length = torch.mean(mel_lengths.float())

    if config.use_speaker_embedding:
        if config.use_external_speaker_embedding_file:
            # return precomputed embedding vector
            speaker_c = data[8]
        else:
            # return speaker_id to be used by an embedding layer
            speaker_c = [speaker_mapping[speaker_name] for speaker_name in speaker_names]
            speaker_c = torch.LongTensor(speaker_c)
    else:
        speaker_c = None

    # dispatch data to GPU
    if use_cuda:
        text_input = text_input.cuda(non_blocking=True)
        text_lengths = text_lengths.cuda(non_blocking=True)
        mel_input = mel_input.cuda(non_blocking=True)
        mel_lengths = mel_lengths.cuda(non_blocking=True)
        if speaker_c is not None:
            speaker_c = speaker_c.cuda(non_blocking=True)
        if attn_mask is not None:
            attn_mask = attn_mask.cuda(non_blocking=True)
    return (
        text_input,
        text_lengths,
        mel_input,
        mel_lengths,
        speaker_c,
        avg_text_length,
        avg_spec_length,
        attn_mask,
        item_idx,
    )


def data_depended_init(data_loader, model):
    """Data depended initialization for activation normalization."""
    if hasattr(model, "module"):
        for f in model.module.decoder.flows:
            if getattr(f, "set_ddi", False):
                f.set_ddi(True)
    else:
        for f in model.decoder.flows:
            if getattr(f, "set_ddi", False):
                f.set_ddi(True)

    model.train()
    print(" > Data depended initialization ... ")
    num_iter = 0
    with torch.no_grad():
        for _, data in enumerate(data_loader):

            # format data
            text_input, text_lengths, mel_input, mel_lengths, spekaer_embed, _, _, attn_mask, _ = format_data(data)

            # forward pass model
            _ = model.forward(text_input, text_lengths, mel_input, mel_lengths, attn_mask, g=spekaer_embed)
            if num_iter == config.data_dep_init_steps:
                break
            num_iter += 1

    if hasattr(model, "module"):
        for f in model.module.decoder.flows:
            if getattr(f, "set_ddi", False):
                f.set_ddi(False)
    else:
        for f in model.decoder.flows:
            if getattr(f, "set_ddi", False):
                f.set_ddi(False)
    return model


def train(data_loader, model, criterion, optimizer, scheduler, ap, global_step, epoch):

    model.train()
    epoch_time = 0
    keep_avg = KeepAverage()
    if use_cuda:
        batch_n_iter = int(len(data_loader.dataset) / (config.batch_size * num_gpus))
    else:
        batch_n_iter = int(len(data_loader.dataset) / config.batch_size)
    end_time = time.time()
    c_logger.print_train_start()
    scaler = torch.cuda.amp.GradScaler() if config.mixed_precision else None
    for num_iter, data in enumerate(data_loader):
        start_time = time.time()

        # format data
        (
            text_input,
            text_lengths,
            mel_input,
            mel_lengths,
            speaker_c,
            avg_text_length,
            avg_spec_length,
            attn_mask,
            _,
        ) = format_data(data)

        loader_time = time.time() - end_time

        global_step += 1
        optimizer.zero_grad()

        # forward pass model
        with torch.cuda.amp.autocast(enabled=config.mixed_precision):
            z, logdet, y_mean, y_log_scale, alignments, o_dur_log, o_total_dur = model.forward(
                text_input, text_lengths, mel_input, mel_lengths, attn_mask, g=speaker_c
            )

            # compute loss
            loss_dict = criterion(z, y_mean, y_log_scale, logdet, mel_lengths, o_dur_log, o_total_dur, text_lengths)

        # backward pass with loss scaling
        if config.mixed_precision:
            scaler.scale(loss_dict["loss"]).backward()
            scaler.unscale_(optimizer)
            grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), config.grad_clip)
            scaler.step(optimizer)
            scaler.update()
        else:
            loss_dict["loss"].backward()
            grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), config.grad_clip)
            optimizer.step()

        # setup lr
        if config.noam_schedule:
            scheduler.step()

        # current_lr
        current_lr = optimizer.param_groups[0]["lr"]

        # compute alignment error (the lower the better )
        align_error = 1 - alignment_diagonal_score(alignments, binary=True)
        loss_dict["align_error"] = align_error

        step_time = time.time() - start_time
        epoch_time += step_time

        # aggregate losses from processes
        if num_gpus > 1:
            loss_dict["log_mle"] = reduce_tensor(loss_dict["log_mle"].data, num_gpus)
            loss_dict["loss_dur"] = reduce_tensor(loss_dict["loss_dur"].data, num_gpus)
            loss_dict["loss"] = reduce_tensor(loss_dict["loss"].data, num_gpus)

        # detach loss values
        loss_dict_new = dict()
        for key, value in loss_dict.items():
            if isinstance(value, (int, float)):
                loss_dict_new[key] = value
            else:
                loss_dict_new[key] = value.item()
        loss_dict = loss_dict_new

        # update avg stats
        update_train_values = dict()
        for key, value in loss_dict.items():
            update_train_values["avg_" + key] = value
        update_train_values["avg_loader_time"] = loader_time
        update_train_values["avg_step_time"] = step_time
        keep_avg.update_values(update_train_values)

        # print training progress
        if global_step % config.print_step == 0:
            log_dict = {
                "avg_spec_length": [avg_spec_length, 1],  # value, precision
                "avg_text_length": [avg_text_length, 1],
                "step_time": [step_time, 4],
                "loader_time": [loader_time, 2],
                "current_lr": current_lr,
            }
            c_logger.print_train_step(batch_n_iter, num_iter, global_step, log_dict, loss_dict, keep_avg.avg_values)

        if args.rank == 0:
            # Plot Training Iter Stats
            # reduce TB load
            if global_step % config.tb_plot_step == 0:
                iter_stats = {"lr": current_lr, "grad_norm": grad_norm, "step_time": step_time}
                iter_stats.update(loss_dict)
                tb_logger.tb_train_iter_stats(global_step, iter_stats)

            if global_step % config.save_step == 0:
                if config.checkpoint:
                    # save model
                    save_checkpoint(
                        model,
                        optimizer,
                        global_step,
                        epoch,
                        1,
                        OUT_PATH,
                        model_characters,
                        model_loss=loss_dict["loss"],
                    )

                # wait all kernels to be completed
                torch.cuda.synchronize()

                # Diagnostic visualizations
                # direct pass on model for spec predictions
                target_speaker = None if speaker_c is None else speaker_c[:1]

                if hasattr(model, "module"):
                    spec_pred, *_ = model.module.inference(text_input[:1], text_lengths[:1], g=target_speaker)
                else:
                    spec_pred, *_ = model.inference(text_input[:1], text_lengths[:1], g=target_speaker)

                spec_pred = spec_pred.permute(0, 2, 1)
                gt_spec = mel_input.permute(0, 2, 1)
                const_spec = spec_pred[0].data.cpu().numpy()
                gt_spec = gt_spec[0].data.cpu().numpy()
                align_img = alignments[0].data.cpu().numpy()

                figures = {
                    "prediction": plot_spectrogram(const_spec, ap),
                    "ground_truth": plot_spectrogram(gt_spec, ap),
                    "alignment": plot_alignment(align_img),
                }

                tb_logger.tb_train_figures(global_step, figures)

                # Sample audio
                train_audio = ap.inv_melspectrogram(const_spec.T)
                tb_logger.tb_train_audios(global_step, {"TrainAudio": train_audio}, config.audio["sample_rate"])
        end_time = time.time()

    # print epoch stats
    c_logger.print_train_epoch_end(global_step, epoch, epoch_time, keep_avg)

    # Plot Epoch Stats
    if args.rank == 0:
        epoch_stats = {"epoch_time": epoch_time}
        epoch_stats.update(keep_avg.avg_values)
        tb_logger.tb_train_epoch_stats(global_step, epoch_stats)
        if config.tb_model_param_stats:
            tb_logger.tb_model_weights(model, global_step)
    return keep_avg.avg_values, global_step


@torch.no_grad()
def evaluate(data_loader, model, criterion, ap, global_step, epoch):
    model.eval()
    epoch_time = 0
    keep_avg = KeepAverage()
    c_logger.print_eval_start()
    if data_loader is not None:
        for num_iter, data in enumerate(data_loader):
            start_time = time.time()

            # format data
            text_input, text_lengths, mel_input, mel_lengths, speaker_c, _, _, attn_mask, _ = format_data(data)

            # forward pass model
            z, logdet, y_mean, y_log_scale, alignments, o_dur_log, o_total_dur = model.forward(
                text_input, text_lengths, mel_input, mel_lengths, attn_mask, g=speaker_c
            )

            # compute loss
            loss_dict = criterion(z, y_mean, y_log_scale, logdet, mel_lengths, o_dur_log, o_total_dur, text_lengths)

            # step time
            step_time = time.time() - start_time
            epoch_time += step_time

            # compute alignment score
            align_error = 1 - alignment_diagonal_score(alignments)
            loss_dict["align_error"] = align_error

            # aggregate losses from processes
            if num_gpus > 1:
                loss_dict["log_mle"] = reduce_tensor(loss_dict["log_mle"].data, num_gpus)
                loss_dict["loss_dur"] = reduce_tensor(loss_dict["loss_dur"].data, num_gpus)
                loss_dict["loss"] = reduce_tensor(loss_dict["loss"].data, num_gpus)

            # detach loss values
            loss_dict_new = dict()
            for key, value in loss_dict.items():
                if isinstance(value, (int, float)):
                    loss_dict_new[key] = value
                else:
                    loss_dict_new[key] = value.item()
            loss_dict = loss_dict_new

            # update avg stats
            update_train_values = dict()
            for key, value in loss_dict.items():
                update_train_values["avg_" + key] = value
            keep_avg.update_values(update_train_values)

            if config.print_eval:
                c_logger.print_eval_step(num_iter, loss_dict, keep_avg.avg_values)

        if args.rank == 0:
            # Diagnostic visualizations
            # direct pass on model for spec predictions
            target_speaker = None if speaker_c is None else speaker_c[:1]
            if hasattr(model, "module"):
                spec_pred, *_ = model.module.inference(text_input[:1], text_lengths[:1], g=target_speaker)
            else:
                spec_pred, *_ = model.inference(text_input[:1], text_lengths[:1], g=target_speaker)
            spec_pred = spec_pred.permute(0, 2, 1)
            gt_spec = mel_input.permute(0, 2, 1)

            const_spec = spec_pred[0].data.cpu().numpy()
            gt_spec = gt_spec[0].data.cpu().numpy()
            align_img = alignments[0].data.cpu().numpy()

            eval_figures = {
                "prediction": plot_spectrogram(const_spec, ap),
                "ground_truth": plot_spectrogram(gt_spec, ap),
                "alignment": plot_alignment(align_img),
            }

            # Sample audio
            eval_audio = ap.inv_melspectrogram(const_spec.T)
            tb_logger.tb_eval_audios(global_step, {"ValAudio": eval_audio}, config.audio["sample_rate"])

            # Plot Validation Stats
            tb_logger.tb_eval_stats(global_step, keep_avg.avg_values)
            tb_logger.tb_eval_figures(global_step, eval_figures)

    if args.rank == 0 and epoch >= config.test_delay_epochs:
        if config.test_sentences_file:
            with open(config.test_sentences_file, "r", -1, "utf-8") as f:
                test_sentences = [s.strip() for s in f.readlines()]
        else:
            test_sentences = [
                "It took me quite a long time to develop a voice, and now that I have it I'm not going to be silent.",
                "Be a voice, not an echo.",
                "I'm sorry Dave. I'm afraid I can't do that.",
                "This cake is great. It's so delicious and moist.",
                "Prior to November 22, 1963.",
            ]

        # test sentences
        test_audios = {}
        test_figures = {}
        print(" | > Synthesizing test sentences")
        if config.use_speaker_embedding:
            if config.use_external_speaker_embedding_file:
                speaker_embedding = speaker_mapping[list(speaker_mapping.keys())[randrange(len(speaker_mapping) - 1)]][
                    "embedding"
                ]
                speaker_id = None
            else:
                speaker_id = 0
                speaker_embedding = None
        else:
            speaker_id = None
            speaker_embedding = None

        style_wav = config.style_wav_for_test
        for idx, test_sentence in enumerate(test_sentences):
            try:
                wav, alignment, _, postnet_output, _, _ = synthesis(
                    model,
                    test_sentence,
                    config,
                    use_cuda,
                    ap,
                    speaker_id=speaker_id,
                    speaker_embedding=speaker_embedding,
                    style_wav=style_wav,
                    truncated=False,
                    enable_eos_bos_chars=config.enable_eos_bos_chars,  # pylint: disable=unused-argument
                    use_griffin_lim=True,
                    do_trim_silence=False,
                )

                file_path = os.path.join(AUDIO_PATH, str(global_step))
                os.makedirs(file_path, exist_ok=True)
                file_path = os.path.join(file_path, "TestSentence_{}.wav".format(idx))
                ap.save_wav(wav, file_path)
                test_audios["{}-audio".format(idx)] = wav
                test_figures["{}-prediction".format(idx)] = plot_spectrogram(postnet_output, ap)
                test_figures["{}-alignment".format(idx)] = plot_alignment(alignment)
            except:  # pylint: disable=bare-except
                print(" !! Error creating Test Sentence -", idx)
                traceback.print_exc()
        tb_logger.tb_test_audios(global_step, test_audios, config.audio["sample_rate"])
        tb_logger.tb_test_figures(global_step, test_figures)
    return keep_avg.avg_values


def main(args):  # pylint: disable=redefined-outer-name
    # pylint: disable=global-variable-undefined
    global meta_data_train, meta_data_eval, symbols, phonemes, model_characters, speaker_mapping
    # Audio processor
    ap = AudioProcessor(**config.audio.to_dict())
    if config.has("characters") and config.characters:
        symbols, phonemes = make_symbols(**config.characters.to_dict())

    # DISTRUBUTED
    if num_gpus > 1:
        init_distributed(args.rank, num_gpus, args.group_id, config.distributed["backend"], config.distributed["url"])

    # set model characters
    model_characters = phonemes if config.use_phonemes else symbols
    num_chars = len(model_characters)

    # load data instances
    meta_data_train, meta_data_eval = load_meta_data(config.datasets)

    # parse speakers
    num_speakers, speaker_embedding_dim, speaker_mapping = parse_speakers(config, args, meta_data_train, OUT_PATH)

    # setup model
    model = setup_model(num_chars, num_speakers, config, speaker_embedding_dim=speaker_embedding_dim)
    optimizer = RAdam(model.parameters(), lr=config.lr, weight_decay=0, betas=(0.9, 0.98), eps=1e-9)
    criterion = GlowTTSLoss()

    if args.restore_path:
        print(f" > Restoring from {os.path.basename(args.restore_path)} ...")
        checkpoint = torch.load(args.restore_path, map_location="cpu")
        try:
            # TODO: fix optimizer init, model.cuda() needs to be called before
            # optimizer restore
            optimizer.load_state_dict(checkpoint["optimizer"])
            model.load_state_dict(checkpoint["model"])
        except:  # pylint: disable=bare-except
            print(" > Partial model initialization.")
            model_dict = model.state_dict()
            model_dict = set_init_dict(model_dict, checkpoint["model"], config)
            model.load_state_dict(model_dict)
            del model_dict

        for group in optimizer.param_groups:
            group["initial_lr"] = config.lr
        print(f" > Model restored from step {checkpoint['step']:d}", flush=True)
        args.restore_step = checkpoint["step"]
    else:
        args.restore_step = 0

    if use_cuda:
        model.cuda()
        criterion.cuda()

    # DISTRUBUTED
    if num_gpus > 1:
        model = DDP_th(model, device_ids=[args.rank])

    if config.noam_schedule:
        scheduler = NoamLR(optimizer, warmup_steps=config.warmup_steps, last_epoch=args.restore_step - 1)
    else:
        scheduler = None

    num_params = count_parameters(model)
    print("\n > Model has {} parameters".format(num_params), flush=True)

    if args.restore_step == 0 or not args.best_path:
        best_loss = float("inf")
        print(" > Starting with inf best loss.")
    else:
        print(" > Restoring best loss from " f"{os.path.basename(args.best_path)} ...")
        best_loss = torch.load(args.best_path, map_location="cpu")["model_loss"]
        print(f" > Starting with loaded last best loss {best_loss}.")
    keep_all_best = config.keep_all_best
    keep_after = config.keep_after  # void if keep_all_best False

    # define dataloaders
    train_loader = setup_loader(ap, 1, is_val=False, verbose=True)
    eval_loader = setup_loader(ap, 1, is_val=True, verbose=True)

    global_step = args.restore_step
    model = data_depended_init(train_loader, model)
    for epoch in range(0, config.epochs):
        c_logger.print_epoch_start(epoch, config.epochs)
        train_avg_loss_dict, global_step = train(
            train_loader, model, criterion, optimizer, scheduler, ap, global_step, epoch
        )
        eval_avg_loss_dict = evaluate(eval_loader, model, criterion, ap, global_step, epoch)
        c_logger.print_epoch_end(epoch, eval_avg_loss_dict)
        target_loss = train_avg_loss_dict["avg_loss"]
        if config.run_eval:
            target_loss = eval_avg_loss_dict["avg_loss"]
        best_loss = save_best_model(
            target_loss,
            best_loss,
            model,
            optimizer,
            global_step,
            epoch,
            config.r,
            OUT_PATH,
            model_characters,
            keep_all_best=keep_all_best,
            keep_after=keep_after,
        )


if __name__ == "__main__":
    args, config, OUT_PATH, AUDIO_PATH, c_logger, tb_logger = init_training(sys.argv)

    try:
        main(args)
    except KeyboardInterrupt:
        remove_experiment_folder(OUT_PATH)
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)  # pylint: disable=protected-access
    except Exception:  # pylint: disable=broad-except
        remove_experiment_folder(OUT_PATH)
        traceback.print_exc()
        sys.exit(1)
