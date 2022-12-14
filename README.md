## Description

# AWS Config - SageMaker Notebook Direct Internet Access Remediation

This script attempts to automatically remediate newly created SageMaker notebooks that become
non-compliant to AWS Config that requires notebooks be within the VPC. `Identifier: SAGEMAKER_NOTEBOOK_NO_DIRECT_INTERNET_ACCESS`

You can read more about this error code [here](https://docs.aws.amazon.com/config/latest/developerguide/sagemaker-notebook-no-direct-internet-access.html)

SageMaker notebooks cannot be edited into a VPC. The notebook must be deleted and a new one created. This script deletes the old notebook, and created a new notebook with the same options within a viable, intenret connected, VPC. It looks for a viable public subnet, and create net new private subnet and security group. It then creates the new notebook.

## Getting Started

- Setup an Amazon EventBridge Rule, and set the json payload to `eventBridgeRuleEvent.json `

- Paste python code into lambda and make it the destinateion of the event rule

