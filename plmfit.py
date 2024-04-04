import torch
from plmfit.logger import Logger
import os
import argparse
from plmfit.models.pretrained_models import *
from plmfit.models.fine_tuning import FullRetrainFineTuner, LowRankAdaptationFineTuner
from plmfit.models.lightning_model import LightningModel
import plmfit.shared_utils.utils as utils
import plmfit.shared_utils.data_explore as data_explore
import plmfit.models.downstream_heads as heads
import traceback
import torch.multiprocessing as mp
from ray import tune
from ray.tune import CLIReporter
from ray.train import RunConfig
from ray.tune.search.bayesopt import BayesOptSearch
from ray.tune.schedulers import ASHAScheduler
from ray.tune.search import ConcurrencyLimiter
import ray
from functools import partial
import time
import lightning as L
from lightning.pytorch.loggers import TensorBoardLogger

parser = argparse.ArgumentParser(description='plmfit_args')
# options ['progen2-small', 'progen2-xlarge', 'progen2-oas', 'progen2-medium', 'progen2-base', 'progen2-BFD90' , 'progen2-large']
parser.add_argument('--plm', type=str, default='progen2-small')
parser.add_argument('--ft_method', type=str, default='feature_extraction')
parser.add_argument('--data_type', type=str, default='aav')
# here you specifcy the different splits
parser.add_argument('--data_file_name', type=str, default='data_train')

parser.add_argument('--head_config', type=str, default='linear_head_config.json')
parser.add_argument('--ray_tuning', type=bool, default=False)

parser.add_argument('--split', type=str, default='') #TODO implement split logic as well

parser.add_argument('--function', type=str, default='extract_embeddings')
parser.add_argument('--reduction', type=str, default='mean',
                    help='Reduction technique')
parser.add_argument('--layer', type=str, default='last',
                    help='PLM layer to be used')
parser.add_argument('--output_dir', type=str, default='default',
                    help='Output directory for created files')
parser.add_argument('--experiment_name', type=str, default='default',
                    help='Output directory for created files')
parser.add_argument('--experiment_dir', type=str, default='default',
                    help='Output directory for created files')

parser.add_argument('--logger', type=str, default='remote')
parser.add_argument('--cpus', default=1)
parser.add_argument('--gpus', default=0)

parser.add_argument('--beta', default=False)

args = parser.parse_args()

experiment_dir = args.experiment_dir
if not os.path.exists(experiment_dir):
    os.makedirs(experiment_dir, exist_ok=True)

# Removing the output_dir prefix from experiment_dir
trimmed_experiment_dir = experiment_dir.removeprefix(f"{args.output_dir}/")
logger = Logger(
    experiment_name = args.experiment_name, 
    base_dir= args.experiment_dir, 
    log_to_server=args.logger!='local', 
    server_path=f'{trimmed_experiment_dir}'
)

def init_plm(model_name, logger):
    model = None
    supported_progen2 = ['progen2-small', 'progen2-medium', 'progen2-xlarge']
    supported_ESM = ["esm2_t6_8M_UR50D", "esm2_t12_35M_UR50D",
                     "esm2_t30_150M_UR50D", "esm2_t33_650M_UR50D","esm2_t36_3B_UR50D"]
    supported_Ankh = ['ankh-base', 'ankh-large', 'ankh2-large']
    supported_Proteinbert = ['proteinbert']

    if 'progen' in model_name:
        assert model_name in supported_progen2, 'Progen version is not supported'
        model = ProGenFamily(model_name, logger)

    elif 'esm' in model_name:
        assert model_name in supported_ESM, 'ESM version is not supported'
        model = ESMFamily(model_name)

    elif 'ankh' in model_name:
        assert model_name in supported_Ankh, 'Ankh version is not supported'
        model = AnkhFamily(model_name)
    elif 'antiberty' in args.plm:
        model = Antiberty()
    elif 'proteinbert' in model_name:
        assert model_name in supported_Proteinbert, 'ProteinBERT version is not supported'
        model = ProteinBERTFamily(logger)
    else:
        raise 'PLM not supported'

    return model

def extract_embeddings(args, logger):
    model = init_plm(args.plm, logger)
    assert model != None, 'Model is not initialized'

    model.extract_embeddings(data_type=args.data_type, layer=args.layer,
                            reduction=args.reduction)

def feature_extraction(config, args, logger, on_ray_tuning=False):
    # Load dataset
    data = utils.load_dataset(args.data_type)
    head_config = config if not on_ray_tuning else utils.adjust_config_to_int(config)
    
    # Load embeddings and scores
    ### TODO : Load embeddings if do not exist
    embeddings = utils.load_embeddings(emb_path=f'{args.output_dir}/extract_embeddings', data_type=args.data_type, model=args.plm, layer=args.layer, reduction=args.reduction)
    assert embeddings != None, "Couldn't find embeddings, you can use extract_embeddings function to save {}"

    scores = data['score'].values if head_config['architecture_parameters']['task'] == 'regression' else data['binary_score'].values
    scores = torch.tensor(scores, dtype=torch.float32)

    training_params = head_config['training_parameters']
    data_loaders = utils.create_data_loaders(
            embeddings, scores, scaler=training_params['scaler'], batch_size=training_params['batch_size'], validation_size=training_params['val_split'], num_workers=0)
    
    logger.save_data(vars(args), 'arguments')
    logger.save_data(head_config, 'head_config')

    network_type = head_config['architecture_parameters']['network_type']
    if network_type == 'linear':
        head_config['architecture_parameters']['input_dim'] = embeddings.shape[1]
        pred_model = heads.LinearHead(head_config['architecture_parameters'])
    elif network_type == 'mlp':
        head_config['architecture_parameters']['input_dim'] = embeddings.shape[1]
        pred_model = heads.MLP(head_config['architecture_parameters'])
    else:
        raise ValueError('Head type not supported')
    
    utils.set_trainable_parameters(pred_model)
    fine_tuner = FullRetrainFineTuner(training_config=training_params, logger=logger)
    fine_tuner.train(pred_model, dataloaders_dict=data_loaders, on_ray_tuning=on_ray_tuning)

def lora(args, logger):
    # Load dataset
    data = utils.load_dataset(args.data_type)
    
    model = init_plm(args.plm, logger)
    assert model != None, 'Model is not initialized'
    
    head_config = utils.load_config(args.head_config)
    
    logger.save_data(vars(args), 'arguments')
    logger.save_data(head_config, 'head_config')
    # data = data.sample(1000)
    network_type = head_config['architecture_parameters']['network_type']
    if network_type == 'linear':
        head_config['architecture_parameters']['input_dim'] = model.emb_layers_dim
        pred_model = heads.LinearHead(head_config['architecture_parameters'])
    elif network_type == 'mlp':
        head_config['architecture_parameters']['input_dim'] = model.emb_layers_dim
        pred_model = heads.MLP(head_config['architecture_parameters'])
    else:
        raise ValueError('Head type not supported')
    
    model.py_model.set_head(pred_model)
    model.py_model.reduction = args.reduction
    model.set_layer_to_use(args.layer)
    model.py_model.layer_to_use = model.layer_to_use
    encs = model.categorical_encode(data)

    scores = data['score'].values if head_config['architecture_parameters']['task'] == 'regression' else data['binary_score'].values
    training_params = head_config['training_parameters']
    data_loaders = utils.create_data_loaders(
            encs, scores, scaler=training_params['scaler'], 
            batch_size=training_params['batch_size'], 
            validation_size=training_params['val_split'], 
            dtype=torch.int8, 
            num_workers=0)
    fine_tuner = LowRankAdaptationFineTuner(training_config=training_params, model_name=args.plm, logger=logger)
    model = fine_tuner.set_trainable_parameters(model)
    model.task = pred_model.task
    fine_tuner.train(model, dataloaders_dict=data_loaders)

def lora_beta(args, logger):
    # Load dataset
    data = utils.load_dataset(args.data_type)

    model = init_plm(args.plm, logger)
    assert model != None, 'Model is not initialized'

    head_config = utils.load_config(args.head_config)
    
    logger.save_data(vars(args), 'arguments')
    logger.save_data(head_config, 'head_config')

    data = data.sample(1000)
    network_type = head_config['architecture_parameters']['network_type']
    if network_type == 'linear':
        head_config['architecture_parameters']['input_dim'] = model.emb_layers_dim
        pred_model = heads.LinearHead(head_config['architecture_parameters'])
    elif network_type == 'mlp':
        head_config['architecture_parameters']['input_dim'] = model.emb_layers_dim
        pred_model = heads.MLP(head_config['architecture_parameters'])
    else:
        raise ValueError('Head type not supported')

    model.py_model.set_head(pred_model)
    model.py_model.reduction = args.reduction
    model.set_layer_to_use(args.layer)
    model.py_model.layer_to_use = model.layer_to_use
    encs = model.categorical_encode(data)

    scores = data['score'].values if head_config['architecture_parameters']['task'] == 'regression' else data['binary_score'].values
    training_params = head_config['training_parameters']
    data_loaders = utils.create_data_loaders(
            encs, scores, scaler=training_params['scaler'], 
            batch_size=training_params['batch_size'], 
            validation_size=training_params['val_split'], 
            dtype=torch.int8, 
            num_workers=0)
    
    fine_tuner = LowRankAdaptationFineTuner(training_config=training_params, model_name=args.plm, logger=logger)
    model = fine_tuner.set_trainable_parameters(model)
    model.py_model.task = pred_model.task
    
    model = LightningModel(model.py_model, head_config['training_parameters'], plmfit_logger=logger)
    lightning_logger = TensorBoardLogger(save_dir=logger.base_dir, version=1, name="lightning_logs")
    
    trainer = L.Trainer(
        logger=lightning_logger, 
        max_epochs=model.hparams.epochs, 
        enable_progress_bar=False, 
        callbacks=model.early_stopping(), 
        accumulate_grad_batches=model.gradient_accumulation_steps(),
        gradient_clip_val=model.gradient_clipping(),
        limit_train_batches=model.epoch_sizing(),
        limit_val_batches=model.epoch_sizing()
    )

    trainer.fit(model, data_loaders['train'], data_loaders['val'])

    trainer.test(ckpt_path="best", dataloaders=data_loaders['test'])

    loss_plot = data_explore.create_loss_plot(json_path=f'{logger.base_dir}/{logger.experiment_name}_loss.json')
    logger.save_plot(loss_plot, "training_validation_loss")

    if pred_model.task == 'classification':
        fig, _ = data_explore.plot_roc_curve(json_path=f'{logger.base_dir}/{logger.experiment_name}_metrics.json')
        logger.save_plot(fig, 'roc_curve')
        fig = data_explore.plot_confusion_matrix_heatmap(json_path=f'{logger.base_dir}/{logger.experiment_name}_metrics.json')
        logger.save_plot(fig, 'confusion_matrix')
    elif pred_model.task == 'regression':
        fig = data_explore.plot_actual_vs_predicted(json_path=f'{logger.base_dir}/{logger.experiment_name}_metrics.json')
        logger.save_plot(fig, 'actual_vs_predicted')

def full_retrain(args, logger):
    # Load dataset
    data = utils.load_dataset(args.data_type)

    model = init_plm(args.plm, logger)
    assert model != None, 'Model is not initialized'
    
    head_config = utils.load_config(args.head_config)
    
    logger.save_data(vars(args), 'arguments')
    logger.save_data(head_config, 'head_config')

    network_type = head_config['architecture_parameters']['network_type']
    if network_type == 'linear':
        head_config['architecture_parameters']['input_dim'] = model.emb_layers_dim
        pred_model = heads.LinearHead(head_config['architecture_parameters'])
    elif network_type == 'mlp':
        head_config['architecture_parameters']['input_dim'] = model.emb_layers_dim
        pred_model = heads.MLP(head_config['architecture_parameters'])
    else:
        raise ValueError('Head type not supported')
    
    utils.freeze_parameters(model.py_model)
    utils.set_trainable_parameters(pred_model)
    model.py_model.set_head(pred_model)
    utils.get_parameters(model.py_model, logger=logger)
    data = data.sample(100000)
    encs = model.categorical_encode(data)
    logger.log(model.py_model)
    scores = data['score'].values if head_config['architecture_parameters']['task'] == 'regression' else data['binary_score'].values
    training_params = head_config['training_parameters']
    data_loaders = utils.create_data_loaders(
            encs, scores, scaler=training_params['scaler'], batch_size=training_params['batch_size'], validation_size=training_params['val_split'], dtype=torch.int8)
    fine_tuner = FullRetrainFineTuner(training_config=training_params, logger=logger)
    model.py_model.task = pred_model.task
    fine_tuner.train(model.py_model, dataloaders_dict=data_loaders)

def ray_tuning(head_config, args, logger):
    network_type = head_config['architecture_parameters']['network_type']
    trials = 100
    if network_type == 'mlp': 
        head_config['architecture_parameters']['hidden_dim'] = tune.uniform(64, 4096)
        head_config['architecture_parameters']['hidden_dropout'] = tune.uniform(0, 0.9)
        trials = 300
    head_config['training_parameters']['learning_rate'] = tune.uniform(1e-6, 1e-4)
    head_config['training_parameters']['batch_size'] = tune.uniform(8, 256)
    head_config['training_parameters']['weight_decay'] = tune.uniform(1e-4, 1e-2)

    initial_epoch_sizing = head_config['training_parameters']['epoch_sizing']
    head_config['training_parameters']['epoch_sizing'] = 0.2 # Sample data to make procedure faster

    # Initialize BayesOptSearch
    searcher = BayesOptSearch(
        utility_kwargs={"kind": "ucb", "kappa": 2.5, "xi": 0.0}, random_search_steps=12
    )
    searcher = ConcurrencyLimiter(searcher, max_concurrent=12)

    scheduler = ASHAScheduler(
        max_t=3,
        grace_period=2,
        reduction_factor=2
    )

    reporter = CLIReporter(max_progress_rows=10)

    logger.log("Initializing ray tuning...")

    max_attempts = 1
    attempt = 0
    success = False
    while attempt < max_attempts and not success:
        try:
            ray.init()
            logger.log("Successfully connected to Ray cluster.")
            success = True
        except Exception as e:
            attempt += 1
            logger.log(f"Attempt {attempt} failed with error: {e}")
            if attempt < max_attempts:
                logger.log("Retrying...")
                time.sleep(5)  # wait for 5 seconds before retrying
    if not success:
        raise 'Error with connecting to ray cluster'

    logger.mute = True # Avoid overpopulating logger with a mixture of training procedures
    tuner = tune.Tuner(
        tune.with_resources(
            tune.with_parameters(feature_extraction, args=args, logger=logger, on_ray_tuning=True),
            resources={"cpu": 1}
        ),
        tune_config=tune.TuneConfig(
            metric="loss", 
            mode="min",
            search_alg=searcher,
            scheduler=scheduler,
            num_samples=trials,
        ),
        run_config=RunConfig(
            progress_reporter=reporter, 
            log_to_file=(f"ray_stdout.log", "ray_stderr.log"),
            storage_path=f'{experiment_dir}/raytune_results'),
        param_space=head_config,
    )
    results = tuner.fit()
    logger.mute = False # Ok, logger can be normal now

    best_result = results.get_best_result("loss", "min")
    best_result.config['training_parameters']['epoch_sizing'] = initial_epoch_sizing
    logger.log(f"Best trial config: {best_result.config}")
    logger.log(f"Best trial metrics: {best_result.metrics}")

    return best_result.config

def feature_extraction_beta(head_config, args, logger):
    # Load dataset
    data = utils.load_dataset(args.data_type)
    
    # Load embeddings and scores
    ### TODO : Load embeddings if do not exist
    embeddings = utils.load_embeddings(emb_path=f'{args.output_dir}/extract_embeddings', data_type=args.data_type, model=args.plm, layer=args.layer, reduction=args.reduction)
    assert embeddings != None, "Couldn't find embeddings, you can use extract_embeddings function to save {}"

    scores = data['score'].values if head_config['architecture_parameters']['task'] == 'regression' else data['binary_score'].values
    scores = torch.tensor(scores, dtype=torch.float32)

    training_params = head_config['training_parameters']
    data_loaders = utils.create_data_loaders(
            embeddings, scores, scaler=training_params['scaler'], batch_size=training_params['batch_size'], validation_size=training_params['val_split'], num_workers=0)
    
    logger.save_data(vars(args), 'arguments')
    logger.save_data(head_config, 'head_config')

    network_type = head_config['architecture_parameters']['network_type']
    if network_type == 'linear':
        head_config['architecture_parameters']['input_dim'] = embeddings.shape[1]
        pred_model = heads.LinearHead(head_config['architecture_parameters'])
    elif network_type == 'mlp':
        head_config['architecture_parameters']['input_dim'] = embeddings.shape[1]
        pred_model = heads.MLP(head_config['architecture_parameters'])
    else:
        raise ValueError('Head type not supported')
    
    utils.set_trainable_parameters(pred_model)
    
    model = LightningModel(pred_model, head_config['training_parameters'], plmfit_logger=logger)
    lightning_logger = TensorBoardLogger(save_dir=logger.base_dir, version=1, name="lightning_logs")
    
    trainer = L.Trainer(
        logger=lightning_logger, 
        max_epochs=model.hparams.epochs, 
        enable_progress_bar=False, 
        callbacks=model.early_stopping(), 
        accumulate_grad_batches=model.gradient_accumulation_steps(),
        gradient_clip_val=model.gradient_clipping(),
        limit_train_batches=model.epoch_sizing(),
        limit_val_batches=model.epoch_sizing()
    )

    trainer.fit(model, data_loaders['train'], data_loaders['val'])

    trainer.test(ckpt_path="best", dataloaders=data_loaders['test'])

    loss_plot = data_explore.create_loss_plot(json_path=f'{logger.base_dir}/{logger.experiment_name}_loss.json')
    logger.save_plot(loss_plot, "training_validation_loss")

    if pred_model.task == 'classification':
        fig, _ = data_explore.plot_roc_curve(json_path=f'{logger.base_dir}/{logger.experiment_name}_metrics.json')
        logger.save_plot(fig, 'roc_curve')
        fig = data_explore.plot_confusion_matrix_heatmap(json_path=f'{logger.base_dir}/{logger.experiment_name}_metrics.json')
        logger.save_plot(fig, 'confusion_matrix')
    elif pred_model.task == 'regression':
        fig = data_explore.plot_actual_vs_predicted(json_path=f'{logger.base_dir}/{logger.experiment_name}_metrics.json')
        logger.save_plot(fig, 'actual_vs_predicted')

if __name__ == '__main__':

    try:
        if args.function == 'extract_embeddings':
            extract_embeddings(args, logger)
        elif args.function == 'fine_tuning':
            if args.ft_method == 'feature_extraction':
                head_config = utils.load_config(args.head_config)
                split = None
                training_params = head_config['training_parameters']
                if "multilabel" in head_config['architecture_parameters']['task']:
                    # TODO : Make multilabel task agnostic
                    scores = data[["mouse","cattle","bat"]].values
                    scores_dict = {0:"mouse",1:"cattle",2:"bat"}
                    split = data["random"].values
                else:
                    scores = data['score'].values if head_config['architecture_parameters']['task'] == 'regression' else data['binary_score'].values
                    scores = torch.tensor(scores, dtype=torch.float32)

                data_loaders = utils.create_data_loaders(
                        embeddings, scores, split = split, scaler=training_params['scaler'], batch_size=training_params['batch_size'], validation_size=training_params['val_split'])

                logger.save_data(vars(args), 'arguments')
                logger.save_data(head_config, 'head_config')

                network_type = head_config['architecture_parameters']['network_type']
                if network_type == 'linear':
                    head_config['architecture_parameters']['input_dim'] = embeddings.shape[1]
                    pred_model = heads.LinearHead(head_config['architecture_parameters'])
                elif network_type == 'mlp':
                    head_config['architecture_parameters']['input_dim'] = embeddings.shape[1]
                    pred_model = heads.MLP(head_config['architecture_parameters'])
                else:
                    raise ValueError('Head type not supported')
                utils.set_trainable_parameters(pred_model)
                fine_tuner = FullRetrainFineTuner(training_config=training_params, logger=logger)
                fine_tuner.train(pred_model, dataloaders_dict=data_loaders)
                
                if args.beta: feature_extraction_beta(head_config, args, logger)
                else:
                    if args.ray_tuning:
                        head_config = ray_tuning(head_config, args, logger)
                    feature_extraction(head_config, args, logger)
            elif args.ft_method == 'lora':
                lora(args, logger) if not args.beta else lora_beta(args, logger)
            elif args.ft_method == 'full':
                full_retrain(args, logger)
            else:
                raise ValueError('Fine Tuning method not supported')
        else:
            raise ValueError('Function is not supported')
        logger.log("\n\nEnd of process", force_send=True)
    except:
        logger.mute = False
        stack_trace = traceback.format_exc()
        logger.log(stack_trace, force_send=True)
