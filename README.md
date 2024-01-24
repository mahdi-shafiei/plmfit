# PLMFit

PLMFit is a framework that facilitates finetuning of  protein Language Models (currently supporting ProGen) on custom/experimental sequence data with one of the following methods. PLMFit is a powerful tool for working with protein sequences and conducting various tasks, including fine-tuning, task-specific head usage, and feature extraction from supported Protein Language Models.

## Table of Contents

- [Installation](#installation)
- [Usage](#usage)
  - [Initialization](#initialization)
  - [Task-Specific Head Concatenation](#task-specific-head-concatenation)
  - [Fine-Tuning](#fine-tuning)
  - [Feature Extraction](#feature-extraction)

## Installation

To use the ProGenPLM package in your project, follow these installation steps:

1. Clone this repository:

   ```shell
   git clone https://github.com/LSSI-ETH/plmfit.git
   ```

2. Navigate to the project directory:

   ```shell
   cd plmfit
   ```

3. Install PLMFit using pip:

   ```shell
   pip install .
   ```


## Usage

This section provides an overview of how to use the PLMFit package for various tasks.

### Initialization

To get started, you'll need to initialize a ProGenPLM model (for ProGen family PLMs):

```python
from models.pretrained_models import ProGenPLM

model = ProGenFamily()
```
**Currently supports**
| PLM          | PLM Family class Name | Versions       | Publication Date | Source Link               | Owner        |
| ------------ | --------------------- | -------------- | ----------------- | ------------------------- | ------------ |
| ProGen  | ProGenFamily()          |    | 2022-01-01        |  | Salesforce     |
| ESM | ESMFamily()          | v2.0, v2.1     | 2022-02-15        | [Source Link 2](link2)    | Meta   |

### Task-Specific Head Concatenation

You can concatenate a task-specific head to the model as follows (for demonstration purposes a simple LinearRegression head is being created):

```python
from models.models import LinearRegression
head = LinearRegression(input_dim=32, output_dim=1) 
model.concat_task_specific_head(head)
```

### Transfer learning

Fine-tuning allows you to train the ProGenPLM model for a specific task. You can specify various training parameters, such as the dataset, number of epochs, learning rate, optimizer, batch size, and more. Here's an example:
(for demonstration purposes the model will be fully_retrained ("full_retrain") on the 'aav' dataset with the correspoding labels)

```python
fine_tuner = FullRetrainFineTuner(epochs = 5 , lr = 0.0006, batch_size = 8,  val_split = 0.2 , log_interval = 1)
model.fine_tune('aav' ,  fine_tuner, 'two_vs_many_split', 'adam' , 'mse')
```


### Feature / Embeddings extraction

To extract embeddings or features from the model, you can use the following code:
(for demonstration purposes the ProGen embeddings (features) will be extracted from the 'aav' dataset from layer 11)

```python
model.extract_embeddings(
    dataset_name='aav',
    layer=11
)
```
Adjust the `dataset_name`, `batch_size`, and `layer` parameters as needed for your specific use case. (See supported data_types)
**Currently supports**
| **Transfer learning method**          | **Class / method **   | **Decription** | **Relevant publication** | **Publication date** |
| ------------ | --------------------- | -------------- | ----------------- | ------------------------- |
| Full retraining | FullRetrainFineTuner(...)           |   |    | [Source Link 1](link1)    | 
| Feature extraction | PLMFamily.extract_embeddings(dataset, layer)         |     |       | [Source Link 2](link2)    | 


Adjust the `dataset_name`, `batch_size`, and `layer` parameters as needed for your specific use case. (See supported data_types and fine_tuning_mode)
## Explainability analysis

| **Explainability analysis function**     | **Decription** | **Relevant publication** | **Publication date** |
| ------------ | --------------------- | -------------- | ----------------- | 
| TBD |       |    |      |     |


**Disclaimer**: Replace 'your-username' with the actual GitHub username or organization name where you intend to host this repository.
