"""
This class is adapted/taken from the Diffusion_TS GitHub repository:

Repository: https://github.com/Y-debug-sys/Diffusion-TS
Author: Xinyu Yuan
License: MIT License

Modifications:
- Integrated conditioning logic using conditioning module
- Warm-up epochs with KL regularization
- Rare vs. non-rare sample handling after GMM fitting
- WANDB logging of reconstruction and KL losses
- TQDM progress bars for epoch and inner loops

Note: Please ensure compliance with the repository's license and credit
the original authors when using or distributing this code.
"""

import copy
import math
import os

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from omegaconf import DictConfig
from sklearn.mixture import GaussianMixture
from tqdm.auto import tqdm

from datasets.utils import prepare_dataloader
from generator.conditioning import ConditioningModule
from generator.diffcharge.network import CNN, Attention
from generator.diffusion_ts.gaussian_diffusion import cosine_beta_schedule

try:
    import wandb
except ImportError:
    wandb = None


def linear_beta_schedule(timesteps, device):
    scale = 1000 / timesteps
    beta_start = scale * 0.0001
    beta_end = scale * 0.02
    return torch.linspace(beta_start, beta_end, timesteps, dtype=torch.float32).to(
        device
    )


def cosine_beta_schedule(timesteps, device, s=0.004):
    steps = timesteps + 1
    x = torch.linspace(0, timesteps, steps, dtype=torch.float32).to(device)
    alphas_cumprod = torch.cos(((x / timesteps) + s) / (1 + s) * math.pi * 0.5) ** 2
    alphas_cumprod = alphas_cumprod / alphas_cumprod[0]
    betas = 1 - (alphas_cumprod[1:] / alphas_cumprod[:-1])
    return torch.clip(betas, 0, 0.999).to(device)


class EMA:
    def __init__(self, model, beta, update_every, device):
        self.model = model
        self.ema_model = copy.deepcopy(model).eval().to(device)
        self.beta = beta
        self.update_every = update_every
        self.step = 0
        self.device = device
        for param in self.ema_model.parameters():
            param.requires_grad = False

    def update(self):
        self.step += 1
        if self.step % self.update_every != 0:
            return
        with torch.no_grad():
            for ema_param, model_param in zip(
                self.ema_model.parameters(), self.model.parameters()
            ):
                ema_param.data.mul_(self.beta).add_(
                    model_param.data, alpha=1.0 - self.beta
                )

    def forward(self, x):
        return self.ema_model(x)


class DDPM(nn.Module):
    def __init__(self, cfg: DictConfig):
        super().__init__()
        self.cfg = cfg
        self.device = cfg.device
        self.n_steps = cfg.model.n_steps
        self.schedule = cfg.model.schedule
        self.beta_start = cfg.model.beta_start
        self.beta_end = cfg.model.beta_end
        self.current_epoch = 0
        self.conditioning_var_n_categories = cfg.dataset.conditioning_vars

        self.conditioning_module = ConditioningModule(
            categorical_dims=cfg.dataset.conditioning_vars,
            embedding_dim=cfg.model.cond_emb_dim,
            device=self.device,
        ).to(self.device)

        if cfg.model.network == "attention":
            self.eps_model = Attention(cfg).to(self.device)
        else:
            self.eps_model = CNN(cfg).to(self.device)

        if self.schedule == "linear":
            self.beta = linear_beta_schedule(self.n_steps, self.device)
        elif self.schedule == "cosine":
            self.beta = cosine_beta_schedule(self.n_steps, self.device)
        else:
            self.beta = (
                torch.linspace(
                    self.beta_start**0.5,
                    self.beta_end**0.5,
                    self.n_steps,
                    device=self.device,
                )
                ** 2
            )

        self.alpha = 1.0 - self.beta
        self.alpha_bar = torch.cumprod(self.alpha, dim=0)
        self.sigma2 = torch.cat(
            (
                torch.tensor([self.beta[0]], device=self.device),
                self.beta[1:] * (1 - self.alpha_bar[:-1]) / (1 - self.alpha_bar[1:]),
            )
        )
        self.optimizer = torch.optim.Adam(
            self.eps_model.parameters(), lr=cfg.model.init_lr
        )
        self.loss_func = nn.MSELoss()

        n_epochs = cfg.model.n_epochs
        p1, p2 = int(0.75 * n_epochs), int(0.9 * n_epochs)
        self.lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(
            self.optimizer, milestones=[p1, p2], gamma=0.1
        )

        self.ema = EMA(
            self.eps_model,
            beta=cfg.model.ema_decay,
            update_every=cfg.model.ema_update_interval,
            device=self.device,
        )
        self.sparse_conditioning_loss_weight = cfg.model.sparse_conditioning_loss_weight
        self.warm_up_epochs = cfg.model.warm_up_epochs
        self.kl_weight = cfg.model.kl_weight
        self.gmm_fitted = False
        self.wandb_enabled = getattr(self.cfg, "wandb_enabled", False)

        if self.wandb_enabled and wandb is not None:
            wandb.init(
                project=cfg.wandb.project,
                entity=cfg.wandb.entity,
                config=cfg,
                dir=cfg.run_dir,
            )

    def gather(self, const, t):
        return const.gather(-1, t).view(-1, 1, 1)

    def q_xt_x0(self, x0, t):
        alpha_bar = self.gather(self.alpha_bar, t)
        mean = alpha_bar.sqrt() * x0
        var = 1 - alpha_bar
        return mean, var

    def q_sample(self, x0, t, eps):
        mean, var = self.q_xt_x0(x0, t)
        return mean + var.sqrt() * eps

    def p_sample_step(self, xt, z, t):
        eps_theta = self.eps_model(torch.cat([xt, z], dim=-1), t)
        alpha_bar = self.gather(self.alpha_bar, t)
        alpha = self.gather(self.alpha, t)
        eps_coef = (1 - alpha) / (1 - alpha_bar).sqrt()
        mean = (xt - eps_coef * eps_theta) / alpha.sqrt()
        var = self.gather(self.sigma2, t)
        z_noise = torch.zeros_like(xt) if (t == 0).all() else torch.randn_like(xt)
        return mean + var.sqrt() * z_noise

    def cal_loss(self, x0, z):
        bsz = x0.shape[0]
        t = torch.randint(0, self.n_steps, (bsz,), device=self.device)
        noise = torch.randn_like(x0)
        xt = self.q_sample(x0, t, eps=noise)
        B, seq_len, input_dim = xt.shape
        cond_dim = z.shape[1]
        z_expanded = z.unsqueeze(1).repeat(1, seq_len, 1)
        inp = torch.cat([xt, z_expanded], dim=-1)
        eps_theta = self.eps_model(inp, t)

        return self.loss_func(noise, eps_theta)

    def fit_gmm(self, loader):
        all_mu = []
        self.conditioning_module.eval()
        with torch.no_grad():
            for x0, cond_vars in loader:
                x0 = x0.to(self.device)
                for k in cond_vars:
                    cond_vars[k] = cond_vars[k].to(self.device)
                _, mu, logvar = self.conditioning_module(cond_vars, sample=False)
                all_mu.append(mu.cpu())
        all_mu = torch.cat(all_mu, dim=0)
        self.conditioning_module.gmm = GaussianMixture(
            n_components=self.conditioning_module.n_components,
            covariance_type="full",
            random_state=42,
        )
        self.conditioning_module.gmm.fit(all_mu.numpy())
        log_probs = self.conditioning_module.gmm.score_samples(all_mu.numpy())
        cutoff = np.percentile(log_probs, 10.0)
        self.conditioning_module.log_prob_threshold = cutoff
        self.gmm_fitted = True

    def train_model(self, train_dataset):
        self.train()
        self.to(self.device)
        train_loader = prepare_dataloader(
            train_dataset, batch_size=self.cfg.model.batch_size, shuffle=True
        )
        n_epochs = self.cfg.model.n_epochs
        for epoch in tqdm(range(n_epochs), desc="DDPM Training"):
            self.current_epoch = epoch + 1
            batch_losses = []
            if self.current_epoch > self.warm_up_epochs:
                for param in self.conditioning_module.parameters():
                    param.requires_grad = False

            loader_it = tqdm(train_loader, desc=f"Epoch {epoch+1}", leave=False)
            for x0, cond_vars in loader_it:
                x0 = x0.to(self.device)
                for k in cond_vars:
                    cond_vars[k] = cond_vars[k].to(self.device)

                z, mu, logvar = self.conditioning_module(cond_vars, sample=False)

                if self.current_epoch <= self.warm_up_epochs:
                    loss_main = self.cal_loss(x0, z)
                    if mu is not None and logvar is not None:
                        kl_loss_val = self.conditioning_module.kl_divergence(mu, logvar)
                        loss_main = loss_main + self.kl_weight * kl_loss_val
                        if self.wandb_enabled and wandb is not None:
                            wandb.log({"Loss/KL": kl_loss_val.item()})
                else:
                    with torch.no_grad():
                        mu_detach = mu.detach()
                        if self.gmm_fitted:
                            rare_mask = (
                                self.conditioning_module.is_rare(mu_detach)
                                .float()
                                .to(self.device)
                            )
                        else:
                            rare_mask = torch.zeros(x0.size(0), device=self.device)

                    rare_idx = (rare_mask == 1.0).nonzero(as_tuple=True)[0]
                    non_rare_idx = (rare_mask == 0.0).nonzero(as_tuple=True)[0]
                    loss_rare = torch.tensor(0.0, device=self.device)
                    loss_non_rare = torch.tensor(0.0, device=self.device)

                    if len(rare_idx) > 0:
                        x0_rare = x0[rare_idx]
                        z_rare = z[rare_idx]
                        loss_rare = self.cal_loss(x0_rare, z_rare)

                    if len(non_rare_idx) > 0:
                        x0_non_rare = x0[non_rare_idx]
                        z_non_rare = z[non_rare_idx]
                        loss_non_rare = self.cal_loss(x0_non_rare, z_non_rare)

                    N_r = rare_mask.sum().item()
                    N_nr = (1 - rare_mask).sum().item()
                    N = x0.size(0)
                    lam = self.sparse_conditioning_loss_weight
                    loss_main = (
                        lam * (N_r / N) * loss_rare
                        + (1 - lam) * (N_nr / N) * loss_non_rare
                    )

                self.optimizer.zero_grad()
                loss_main.backward()
                self.optimizer.step()
                self.ema.update()

                batch_losses.append(loss_main.item())
                if self.wandb_enabled and wandb is not None:
                    wandb.log({"Loss/reconstruction": loss_main.item()})

            epoch_loss = sum(batch_losses) / len(batch_losses)
            loader_it.set_postfix({"Epoch Loss": epoch_loss})
            print(f"Epoch {epoch+1}/{n_epochs}, Loss: {epoch_loss:.4f}")
            self.lr_scheduler.step(epoch_loss)

            if self.current_epoch == self.warm_up_epochs and not self.gmm_fitted:
                self.fit_gmm(train_loader)

            if (epoch + 1) % self.cfg.model.save_cycle == 0:
                self.save(epoch=self.current_epoch)

        print("Training complete")

    @torch.no_grad()
    def sample(self, shape, cond_vars, use_ema=False):
        model = self.ema.ema_model if use_ema else self.eps_model
        device = self.device
        x = torch.randn(shape, device=device)
        z, mu, logvar = self.conditioning_module(cond_vars, sample=True)
        for t in tqdm(reversed(range(self.n_steps)), desc="DDPM Sampling"):
            t_tensor = torch.full((x.shape[0],), t, device=device, dtype=torch.long)
            eps_theta = model(torch.cat([x, z], dim=-1), t_tensor)
            alpha_bar = self.gather(self.alpha_bar, t_tensor)
            alpha = self.gather(self.alpha, t_tensor)
            eps_coef = (1 - alpha) / (1 - alpha_bar).sqrt()
            mean = (x - eps_coef * eps_theta) / alpha.sqrt()
            var = self.gather(self.sigma2, t_tensor)
            noise = torch.zeros_like(x) if t == 0 else torch.randn_like(x)
            x = mean + var.sqrt() * noise
        return x

    def gather(self, const, t):
        return const.gather(-1, t).view(-1, 1, 1)

    def save(self, path: str = None, epoch: int = None):
        if path is None:
            hydra_output_dir = os.path.join(self.cfg.run_dir)
            if not os.path.exists(os.path.join(hydra_output_dir, "checkpoints")):
                os.makedirs(
                    os.path.join(hydra_output_dir, "checkpoints"), exist_ok=True
                )
            path = os.path.join(
                hydra_output_dir,
                "checkpoints",
                f"ddpm_checkpoint_{epoch if epoch else self.current_epoch}.pt",
            )
        model_sd = {k: v.cpu() for k, v in self.eps_model.state_dict().items()}
        opt_sd = {
            k: v.cpu() if isinstance(v, torch.Tensor) else v
            for k, v in self.optimizer.state_dict().items()
        }
        ema_sd = {k: v.cpu() for k, v in self.ema.ema_model.state_dict().items()}
        cond_sd = self.conditioning_module.state_dict()
        torch.save(
            {
                "epoch": epoch if epoch is not None else self.current_epoch,
                "eps_model_state_dict": model_sd,
                "optimizer_state_dict": opt_sd,
                "ema_state_dict": ema_sd,
                "alpha_bar": self.alpha_bar.cpu(),
                "beta": self.beta.cpu(),
                "conditioning_module_state_dict": cond_sd,
            },
            path,
        )
        print(f"DDPM checkpoint saved to {path}")

    def load(self, path: str):
        if not os.path.exists(path):
            raise FileNotFoundError(f"Checkpoint not found at {path}")
        ckp = torch.load(path, map_location=self.device)
        if "eps_model_state_dict" in ckp:
            self.eps_model.load_state_dict(ckp["eps_model_state_dict"])
            print("Loaded eps_model state.")
        if "optimizer_state_dict" in ckp:
            self.optimizer.load_state_dict(ckp["optimizer_state_dict"])
            print("Loaded optimizer state.")
        if "ema_state_dict" in ckp:
            self.ema.ema_model.load_state_dict(ckp["ema_state_dict"])
            print("Loaded EMA model state.")
        if "alpha_bar" in ckp:
            self.alpha_bar = ckp["alpha_bar"].to(self.device)
            print("Loaded alpha_bar.")
        if "beta" in ckp:
            self.beta = ckp["beta"].to(self.device)
            print("Loaded beta.")
        if "conditioning_module_state_dict" in ckp:
            self.conditioning_module.load_state_dict(
                ckp["conditioning_module_state_dict"]
            )
            print("Loaded conditioning module state.")
        if "epoch" in ckp:
            self.current_epoch = ckp["epoch"]
            print(f"Loaded epoch number: {self.current_epoch}")
        self.eps_model.to(self.device)
        self.conditioning_module.to(self.device)
        self.ema.ema_model.to(self.device)
        print(f"DDPM loaded and moved to {self.device}.")

    def generate(self, conditioning_vars):
        bs = self.cfg.model.sampling_batch_size
        total = len(next(iter(conditioning_vars.values())))
        generated_samples = []

        for start_idx in range(0, total, bs):
            end_idx = min(start_idx + bs, total)
            batch_conditioning_vars = {
                var_name: var_tensor[start_idx:end_idx]
                for var_name, var_tensor in conditioning_vars.items()
            }
            current_bs = end_idx - start_idx
            shape = (
                current_bs,
                self.n_steps,
                self.n_steps,
            )  # Adjusted based on your model's requirements

            if getattr(self.cfg.model, "use_ema_sampling", False):
                samples = self.ema.ema_model.sample(
                    shape, batch_conditioning_vars, use_ema=True
                )
            else:
                samples = self.sample(shape, batch_conditioning_vars, use_ema=False)

            generated_samples.append(samples)

        return torch.cat(generated_samples, dim=0)
