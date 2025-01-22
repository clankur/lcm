import time
from clearml import Task
import numpy as np
import hydra
import jax_extra
import subprocess
from dataclasses import dataclass
from typing import Optional, Callable, Iterable, List, Dict, Tuple
from datetime import datetime
import json
import itertools


@dataclass
class Config:
    queue_name: str
    project_name: Optional[str] = None
    model_name: Optional[str] = None
    template_id: Optional[str] = None


def get_task_details(config: Config):
    git_branch_name = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    config_name = hydra.core.hydra_config.HydraConfig.get()["job"]["config_name"]
    project_name = (
        config.project_name
        if config.project_name
        else f"{config_name}/{git_branch_name}"
    )

    task_name = config.model_name

    return project_name, task_name


def exponential_moving_average(data, alpha=0.03):
    """
    Compute exponential moving average using vectorized operations.
    alpha = 1 - smoothing_factor
    So for 0.97 smoothing, alpha = 1 - 0.97 = 0.03
    """
    weights = (1 - alpha) ** np.arange(len(data))
    weights /= weights.sum()
    ema = np.convolve(data, weights, mode="full")[: len(data)]
    return ema


def train_model(
    params: Dict, template_id: str, queue_name: str, model_name: str
) -> Tuple[float, str]:
    """Train a model with given parameters and return its loss and task ID."""
    # Clone the template task and override the parameters
    param_str = "_".join(f"{k}:{v}" for k, v in params.items())
    child_task: Task = Task.clone(
        source_task=template_id,
        name=f"{model_name}_{param_str}",
    )
    child_task.set_system_tags([])

    # Set all parameters
    for key, value in params.items():
        if key == "learning_rate":
            child_task.set_parameter("Hydra/training.learning_rate", value)
        elif key == "d_model":
            child_task.set_parameter("Hydra/base.d_model", value)
        elif key == "d_ff":
            child_task.set_parameter("Hydra/base.d_ff", value)

    print(f"Training model with parameters: {params}")
    Task.enqueue(child_task.id, queue_name=queue_name)
    child_task.wait_for_status(check_interval_sec=120)

    # Get the loss from the child task
    scalars = child_task.get_reported_scalars()
    loss = scalars["loss"]["loss"]["y"]
    eval_loss = scalars["final_loss"]["loss"]["y"][-1]
    smoothed_loss = exponential_moving_average(loss, alpha=1 - 0.97)
    return eval_loss, smoothed_loss[-1], child_task.id


def architecture_sweep(
    config_name: str,
    model_name: str,
    queue_name: str,
    template_id: str,
    d_models: List[int] = [256, 384, 512],
    d_ff_multipliers: List[int] = [4, 8, 12],
    learning_rates: List[float] = [5e-3, 1e-2, 2.5e-2, 3e-2, 5e-2],
) -> Dict:
    """
    Perform a sweep over d_model, d_ff, and learning rate configurations.
    Returns the best configuration found.
    """
    project_name = f"{config_name}/arch_sweep"
    task_name = f"{model_name}_arch_sweep_{datetime.now().strftime('%Y%m%d_%H%M')}"
    parent_task = Task.init(project_name=project_name, task_name=task_name)
    logger = parent_task.get_logger()

    results = {}
    best_config = None
    best_loss = float("inf")
    iteration = 0

    # Dictionary to store results by architecture configuration
    arch_results = {}

    # Generate all possible combinations
    for d_model in d_models:
        for d_ff_mult in d_ff_multipliers:
            d_ff = d_model * d_ff_mult
            arch_key = f"d_model={d_model}_d_ff={d_ff}"
            arch_results[arch_key] = {
                "lr_results": [],
                "best_lr": None,
                "best_loss": float("inf"),
            }

            for lr in learning_rates:
                params = {"d_model": d_model, "d_ff": d_ff, "learning_rate": lr}

                # Train the model and get results
                loss, train_loss, task_id = train_model(
                    params, template_id, queue_name, model_name
                )

                # Store results
                config_key = json.dumps(params)
                results[config_key] = {
                    "eval_loss": loss,
                    "train_loss": train_loss,
                    "task_id": task_id,
                }

                # Update best configuration if necessary
                if loss < best_loss:
                    best_loss = loss
                    best_config = params

                # Store results for this architecture configuration
                arch_results[arch_key]["lr_results"].append({"lr": lr, "loss": loss})
                if loss < arch_results[arch_key]["best_loss"]:
                    arch_results[arch_key]["best_loss"] = loss
                    arch_results[arch_key]["best_lr"] = lr

                # Log metrics in multiple ways
                # 1. Overall metrics
                logger.report_scalar("overall/loss", "value", loss, iteration=iteration)
                logger.report_scalar(
                    "overall/best_loss", "value", best_loss, iteration=iteration
                )

                # 2. Architecture-specific metrics
                logger.report_scalar(
                    f"arch_{arch_key}/loss", "value", loss, iteration=iteration
                )
                logger.report_scalar(
                    f"arch_{arch_key}/learning_rate", "value", lr, iteration=iteration
                )
                logger.report_scalar(
                    f"arch_{arch_key}/best_loss",
                    "value",
                    arch_results[arch_key]["best_loss"],
                    iteration=iteration,
                )

                iteration += 1

            # Print current iteration results
            print(
                f"Best loss for this architecture: {arch_results[arch_key]['best_loss']:.6f}"
            )
            print(f"Overall best loss: {best_loss:.6f}")
            print("-" * 50)

    # Print final results grouped by architecture
    print("\nSweep completed!")
    print("\nResults by architecture configuration:")
    for arch_key, arch_data in arch_results.items():
        print(f"\n{arch_key}:")
        print(f"Best learning rate: {arch_data['best_lr']}")
        print(f"Best loss: {arch_data['best_loss']:.6f}")
        print("Learning rate sweep results:")
        for result in sorted(arch_data["lr_results"], key=lambda x: x["lr"]):
            print(f"  lr: {result['lr']:.6f}, loss: {result['loss']:.6f}")
        print("-" * 50)

    print(f"\nOverall best configuration:")
    print(json.dumps(best_config, indent=2))
    print(f"Best loss achieved: {best_loss:.6f}")

    # Save detailed results to the task
    parent_task.get_logger().report_text(
        "detailed_results", json.dumps(arch_results, indent=2)
    )

    parent_task.close()
    return best_config


@hydra.main(config_path="configs", version_base=None)
def main(config):
    config = jax_extra.make_dataclass_from_dict(Config, config)
    config_name = hydra.core.hydra_config.HydraConfig.get()["job"]["config_name"]

    if config.template_id:
        template_id = config.template_id
    else:
        project_name, task_name = get_task_details(config)
        print(f"{project_name=}")
        print(f"{task_name=}")
        template_id = Task.get_task(
            project_name=project_name,
            task_name=task_name,
        ).id

    model_name = config.model_name or config_name

    architecture_sweep(
        config_name=config_name,
        model_name=model_name,
        queue_name=config.queue_name,
        template_id=template_id,
    )


if __name__ == "__main__":
    main()