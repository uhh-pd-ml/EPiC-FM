"""Do final evaluation of the model after training.

Specific to JetNet dataset.
"""

from typing import Mapping, Optional

import h5py
import numpy as np
import pytorch_lightning as pl
import torch
import wandb
import yaml

from src.data.components import calculate_all_wasserstein_metrics, normalize_tensor
from src.data.components.metrics import wasserstein_distance_batched
from src.utils.data_generation import generate_data
from src.utils.jet_substructure import dump_hlvs
from src.utils.plotting import (
    apply_mpl_styles,
    plot_data,
    plot_full_substructure,
    plot_substructure,
    prepare_data_for_plotting,
)
from src.utils.pylogger import get_pylogger

from .ema import EMA, EMAModelCheckpoint

log = get_pylogger("JetNetFinalEvaluationCallback")


# TODO cond_path is currently only working for mass and pt
class JetNetFinalEvaluationCallback(pl.Callback):
    """Callback to do final evaluation of the model after training. Specific to JetNet dataset.

    Args:
        use_ema (bool, optional): Use exponential moving average weights for logging. Defaults to False.
        dataset (str, optional): Dataset to evaluate on. Defaults to "test".
        nr_checkpoint_callbacks (int, optional): Number of checkpoint callback that is used to select best epoch. Will only be used when ckpt_path is None. Defaults to 0.
        use_last_checkpoint (bool, optional): Use last checkpoint instead of best checkpoint. Defaults to True.
        ckpt_path (Optional[str], optional): Path to checkpoint. If given, this ckpt will be used for evaluation. Defaults to None.
        num_jet_samples (int, optional): How many jet samples to generate. Negative values define the amount of times the whole dataset is taken, e.g. -2 would use 2*len(dataset) samples. Defaults to -1.
        fix_seed (bool, optional): Fix seed for data generation to have better reproducibility and comparability between epochs. Defaults to True.
        evaluate_substructure (bool, optional): Evaluate substructure metrics. Takes very long. Defaults to True.
        suffix (str, optional): Suffix for logging. Defaults to "".
        cond_path (Optional[str], optional): Path for conditioning that is used during generation. If not provided, the selected dataset will be used for conditioning. Defaults to None.
        w_dist_config (Mapping, optional): Configuration for Wasserstein distance calculation. Defaults to {'num_jet_samples': 10_000, 'num_batches': 40}.
        generation_config (Mapping, optional): Configuration for data generation. Defaults to {"batch_size": 256, "ode_solver": "midpoint", "ode_steps": 100}.
        plot_config (Mapping, optional): Configuration for plotting. Defaults to {}.
    """

    def __init__(
        self,
        use_ema: bool = True,
        dataset: str = "test",
        nr_checkpoint_callbacks: int = 0,
        use_last_checkpoint: bool = True,
        ckpt_path: Optional[str] = None,
        num_jet_samples: int = -1,
        fix_seed: bool = True,
        evaluate_substructure: bool = True,
        suffix: str = "",
        cond_path: Optional[str] = None,
        w_dist_config: Mapping = {
            "num_eval_samples": 50_000,
            "num_batches": 40,
        },
        generation_config: Mapping = {
            "batch_size": 1024,
            "ode_solver": "midpoint",
            "ode_steps": 100,
        },
        plot_config: Mapping = {"plot_efps": False},
    ):
        super().__init__()

        apply_mpl_styles()

        self.use_ema = use_ema
        self.dataset = dataset
        self.ckpt_path = ckpt_path
        self.nr_checkpoint_callbacks = nr_checkpoint_callbacks
        self.use_last_checkpoint = use_last_checkpoint
        self.num_jet_samples = num_jet_samples
        self.fix_seed = fix_seed
        self.evaluate_substructure = evaluate_substructure
        self.suffix = suffix
        self.cond_path = cond_path
        # loggers
        self.comet_logger = None
        self.wandb_logger = None

        # configs
        self.w_dist_config = w_dist_config
        self.generation_config = generation_config
        self.plot_config = plot_config

    def on_test_start(self, trainer, pl_module) -> None:
        log.info(
            "JetNetFinalEvaluationCallback will be used for evaluating the model after training."
        )

        # set number of jet samples if negative
        if self.num_jet_samples < 0:
            self.datasets_multiplier = abs(self.num_jet_samples)
            if self.dataset == "test":
                self.num_jet_samples = len(trainer.datamodule.tensor_test) * abs(
                    self.num_jet_samples
                )
            if self.dataset == "val":
                self.num_jet_samples = len(trainer.datamodule.tensor_val) * abs(
                    self.num_jet_samples
                )
        else:
            self.datasets_multiplier = -1
        if self.cond_path is not None:
            self.datasets_multiplier = -1

        # get loggers
        for logger in trainer.loggers:
            if isinstance(logger, pl.loggers.CometLogger):
                self.comet_logger = logger.experiment
            elif isinstance(logger, pl.loggers.WandbLogger):
                self.wandb_logger = logger.experiment

    def _get_ema_callback(self, trainer: "pl.Trainer") -> Optional[EMA]:
        ema_callback = None
        for callback in trainer.callbacks:
            if isinstance(callback, EMA):
                ema_callback = callback
        return ema_callback

    def on_test_end(self, trainer: pl.Trainer, pl_module: pl.LightningModule) -> None:
        log.info(f"Evaluating model on {self.dataset} dataset.")

        ckpt = self._get_checkpoint(trainer, use_last_checkpoint=self.use_last_checkpoint)

        log.info(f"Loading checkpoint from {ckpt}")
        model = pl_module.load_from_checkpoint(ckpt)

        if self.fix_seed:
            # fix seed for better reproducibility and comparable results
            torch.manual_seed(9999)

        # load conditioning data if provided
        if self.cond_path is not None:
            with h5py.File(self.cond_path) as f:
                pt_c = f["pt"][:]
                mass_c = f["mass"][:]
                num_particles_c = f["num_particles"][:].squeeze()

            # masking for jet size
            jet_size = trainer.datamodule.hparams.num_particles
            num_particles_ctemp = np.array(
                [n if n <= jet_size else jet_size for n in num_particles_c]
            )

            mask_c = np.expand_dims(
                np.tri(jet_size)[num_particles_ctemp.astype(int) - 1], axis=-1
            ).astype(np.float32)

            # get conditioning data
            # TODO implement other conditioning options
            if trainer.datamodule.num_cond_features != 0:
                cond_means = np.array(trainer.datamodule.cond_means)
                cond_stds = np.array(trainer.datamodule.cond_stds)
                pt_norm = normalize_tensor(pt_c.copy(), [cond_means[0]], [cond_stds[0]])
                mass_norm = normalize_tensor(mass_c.copy(), [cond_means[1]], [cond_stds[1]])
                cond_c = np.concatenate((pt_norm, mass_norm), axis=-1)
            else:
                cond_c = np.concatenate((pt_c, mass_c), axis=-1)

        # Get background data for plotting and calculating Wasserstein distances
        if self.dataset == "test":
            background_data = np.array(trainer.datamodule.tensor_test)[: self.num_jet_samples]
            background_mask = np.array(trainer.datamodule.mask_test)[: self.num_jet_samples]
            background_cond = np.array(trainer.datamodule.tensor_conditioning_test)[
                : self.num_jet_samples
            ]
        elif self.dataset == "val":
            background_data = np.array(trainer.datamodule.tensor_val)[: self.num_jet_samples]
            background_mask = np.array(trainer.datamodule.mask_val)[: self.num_jet_samples]
            background_cond = np.array(trainer.datamodule.tensor_conditioning_val)[
                : self.num_jet_samples
            ]
        if self.cond_path is not None:
            mask = mask_c[: self.num_jet_samples]
            cond = cond_c[: self.num_jet_samples]
        else:
            mask = background_mask
            cond = background_cond

        # maximum number of samples to plot is the number of samples in the dataset
        num_plot_samples = len(background_data)

        if self.datasets_multiplier > 1:
            mask = np.repeat(mask, self.datasets_multiplier, axis=0)
            cond = np.repeat(cond, self.datasets_multiplier, axis=0)

        # Generate data
        data, generation_time = generate_data(
            model=model,
            num_jet_samples=len(mask),
            cond=torch.tensor(cond),
            variable_set_sizes=trainer.datamodule.hparams.variable_jet_sizes,
            mask=torch.tensor(mask),
            normalized_data=trainer.datamodule.hparams.normalize,
            means=trainer.datamodule.means,
            stds=trainer.datamodule.stds,
            **self.generation_config,
        )

        # save generated data
        path = "/".join(ckpt.split("/")[:-2]) + "/"
        file_name = f"final_generated_data{self.suffix}.npy"
        full_path = path + file_name
        np.save(full_path, data)

        # Wasserstein distances
        metrics = calculate_all_wasserstein_metrics(background_data, data, **self.w_dist_config)

        # Prepare Data for Plotting
        data_plotting = data[:num_plot_samples]
        plot_prep_config = {
            "calculate_efps" if key == "plot_efps" else key: value
            for key, value in self.plot_config.items()
            if key in ["plot_efps", "selected_particles", "selected_multiplicities"]
        }

        (
            jet_data,
            efps_values,
            pt_selected_particles,
            pt_selected_multiplicities,
        ) = prepare_data_for_plotting(np.array([data_plotting]), **plot_prep_config)

        (
            jet_data_sim,
            efps_sim,
            pt_selected_particles_sim,
            pt_selected_multiplicities_sim,
        ) = prepare_data_for_plotting(
            [background_data],
            **plot_prep_config,
        )
        jet_data_sim, efps_sim, pt_selected_particles_sim = (
            jet_data_sim[0],
            efps_sim[0],
            pt_selected_particles_sim[0],
        )

        # Plotting
        plot_name = f"final_plot{self.suffix}"
        img_path = "/".join(ckpt.split("/")[:-2]) + "/"
        fig = plot_data(
            particle_data=np.array([data_plotting]),
            sim_data=background_data,
            jet_data_sim=jet_data_sim,
            jet_data=jet_data,
            efps_sim=efps_sim,
            efps_values=efps_values,
            num_samples=num_plot_samples,
            pt_selected_particles=pt_selected_particles,
            pt_selected_multiplicities=pt_selected_multiplicities,
            pt_selected_particles_sim=pt_selected_particles_sim,
            pt_selected_multiplicities_sim=pt_selected_multiplicities_sim,
            save_fig=True,
            save_folder=img_path,
            save_name=plot_name,
            close_fig=True,
            **self.plot_config,
        )

        if self.evaluate_substructure:
            substructure_path = "/".join(ckpt.split("/")[:-2]) + "/"
            substructure_file_name = f"substructure{self.suffix}"
            substructure_full_path = substructure_path + substructure_file_name

            # calculate substructure for generated data
            dump_hlvs(data, substructure_full_path, plot=False)

            substructure_path_jetnet = "/".join(ckpt.split("/")[:-2]) + "/"
            substructure_file_name_jetnet = f"substructure_jetnet{self.suffix}"
            substructure_full_path_jetnet = (
                substructure_path_jetnet + substructure_file_name_jetnet
            )

            # calculate substructure for reference data
            dump_hlvs(background_data, substructure_full_path_jetnet, plot=False)

            # load substructure for model generated data
            keys = []
            data_substructure = []
            with h5py.File(substructure_full_path + ".h5", "r") as f:
                tau21 = np.array(f["tau21"])
                tau32 = np.array(f["tau32"])
                d2 = np.array(f["d2"])
                for key in f.keys():
                    keys.append(key)
                    data_substructure.append(np.array(f[key]))
            keys = np.array(keys)
            data_substructure = np.array(data_substructure)

            # load substructure for JetNet data
            data_substructure_jetnet = []
            with h5py.File(substructure_full_path_jetnet + ".h5", "r") as f:
                tau21_jetnet = np.array(f["tau21"])
                tau32_jetnet = np.array(f["tau32"])
                d2_jetnet = np.array(f["d2"])
                for key in f.keys():
                    data_substructure_jetnet.append(np.array(f[key]))
            data_substructure_jetnet = np.array(data_substructure_jetnet)

            # calculate wasserstein distances
            w_dist_tau21_mean, w_dist_tau21_std = wasserstein_distance_batched(
                tau21_jetnet, tau21, **self.w_dist_config
            )
            w_dist_tau32_mean, w_dist_tau32_std = wasserstein_distance_batched(
                tau32_jetnet, tau32, **self.w_dist_config
            )
            w_dist_d2_mean, w_dist_d2_std = wasserstein_distance_batched(
                d2_jetnet, d2, **self.w_dist_config
            )

            # add to metrics
            metrics["w_dist_tau21_mean"] = w_dist_tau21_mean
            metrics["w_dist_tau21_std"] = w_dist_tau21_std
            metrics["w_dist_tau32_mean"] = w_dist_tau32_mean
            metrics["w_dist_tau32_std"] = w_dist_tau32_std
            metrics["w_dist_d2_mean"] = w_dist_d2_mean
            metrics["w_dist_d2_std"] = w_dist_d2_std

            # plot substructure
            file_name_substructure = f"substructure_3plots{self.suffix}"
            file_name_full_substructure = f"substructure_full{self.suffix}"
            plot_substructure(
                tau21=tau21,
                tau32=tau32,
                d2=d2,
                tau21_jetnet=tau21_jetnet,
                tau32_jetnet=tau32_jetnet,
                d2_jetnet=d2_jetnet,
                save_fig=True,
                save_folder=img_path,
                save_name=file_name_substructure,
                close_fig=True,
            )
            plot_full_substructure(
                data_substructure=data_substructure,
                data_substructure_jetnet=data_substructure_jetnet,
                keys=keys,
                save_fig=True,
                save_folder=img_path,
                save_name=file_name_full_substructure,
                close_fig=True,
            )

            # log substructure images
            img_path_substructure = f"{img_path}{file_name_substructure}.png"
            img_path_substructure_full = f"{img_path}{file_name_full_substructure}.png"
            if self.comet_logger is not None:
                self.comet_logger.log_image(
                    img_path_substructure, name=f"A_final_substructure{self.suffix}"
                )
                self.comet_logger.log_image(
                    img_path_substructure_full, name=f"A_final_substructure_full{self.suffix}"
                )
            if self.wandb_logger is not None:
                self.wandb_logger.log(
                    {f"A_final_substructure{self.suffix}": wandb.Image(img_path_substructure)}
                )
                self.wandb_logger.log(
                    {
                        f"A_final_substructure_full{self.suffix}": wandb.Image(
                            img_path_substructure_full
                        )
                    }
                )

        yaml_path = "/".join(ckpt.split("/")[:-2]) + f"/final_eval_metrics{self.suffix}.yml"
        log.info(f"Writing final evaluation metrics to {yaml_path}")

        # transform numpy.float64 for better readability in yaml file
        metrics = {k: float(v) for k, v in metrics.items()}
        # write to yaml file
        with open(yaml_path, "w") as outfile:
            yaml.dump(metrics, outfile, default_flow_style=False)

        # rename wasserstein distances for better distinction
        metrics_final = {}
        for key, value in metrics.items():
            metrics_final[key + f"_final{self.suffix}"] = value

        # log metrics and image to loggers
        img_path_data = f"{img_path}{plot_name}.png"
        if self.comet_logger is not None:
            self.comet_logger.log_image(img_path_data, name=f"A_final_plot{self.suffix}")
            self.comet_logger.log_metrics(metrics_final)
        if self.wandb_logger is not None:
            self.wandb_logger.log({f"A_final_plot{self.suffix}": wandb.Image(img_path_data)})
            self.wandb_logger.log(metrics_final)

    def _get_checkpoint(self, trainer: pl.Trainer, use_last_checkpoint: bool = True) -> None:
        """Get checkpoint path based on the selected checkpoint callback."""
        if self.ckpt_path is None:
            if self.use_ema:
                if (
                    type(trainer.checkpoint_callbacks[self.nr_checkpoint_callbacks])
                    == EMAModelCheckpoint
                ):
                    if use_last_checkpoint:
                        return trainer.checkpoint_callbacks[
                            self.nr_checkpoint_callbacks
                        ].last_model_path_ema
                    else:
                        return trainer.checkpoint_callbacks[
                            self.nr_checkpoint_callbacks
                        ].best_model_path_ema

                else:
                    raise ValueError(
                        "JetNetFinalEvaluationCallback was told to use EMA weights for evaluation but the provided checkpoint callback is not of type EMAModelCheckpoint"
                    )
            else:
                if use_last_checkpoint:
                    return trainer.checkpoint_callbacks[
                        self.nr_checkpoint_callbacks
                    ].last_model_path
                else:
                    return trainer.checkpoint_callbacks[
                        self.nr_checkpoint_callbacks
                    ].best_model_path
        else:
            return self.ckpt_path
