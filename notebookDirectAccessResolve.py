import boto3
import botocore
from botocore.config import Config
from botocore.exceptions import ClientError
import json
import base64
import time
import logging
import re
from random import *


logger = logging.getLogger()
logger.setLevel(logging.INFO)


config = Config(
   retries = {
      'max_attempts': 10,
      'mode': 'standard'
   }
)


iam = boto3.client('iam', config=config)
sgm = boto3.client('sagemaker', config=config)
s3 = boto3.client('s3', config=config)
ec2 = boto3.client('ec2', config=config,  region_name='us-east-1')
    

def stopNotebook(notebookInstanceName):
    # Check if notebook is running
    notebookStatus = 'Pending'
    notebookDesc = ''
    try:
        sgm.stop_notebook_instance(NotebookInstanceName=notebookInstanceName)
        notebookStatus = 'Stopped'
        logger.info(f'|SageMaker|Stopped instance: {notebookInstanceName}|')
    except ClientError as e:
        logger.info(f'|SageMaker|{e.response["Error"]["Message"]}|')
        if 'Status (Stopped) not in ([InService])' in e.response['Error']['Message']:
            notebookStatus = 'Stopped'
        
    # Dont continue unless notebook stopped
    while notebookStatus != 'Stopped':
        time.sleep(10)
        notebookDesc = sgm.describe_notebook_instance(NotebookInstanceName=notebookInstanceName)
        notebookStatus = notebookDesc['NotebookInstanceStatus']
        # If notebook in service try to stop again
        if notebookStatus == 'InService':
            sgm.stop_notebook_instance(NotebookInstanceName=notebookInstanceName)
        logger.info(f'|SageMaker|Notebook not stopped yet... current status: {notebookStatus} - retrying|')
        
    return notebookDesc
        

def getViableSubnet():        
    # Check NAT gateways in account
    natGateResp = ec2.describe_nat_gateways()

    # Check internet gateways in account
    intGateResp = ec2.describe_internet_gateways()
    
    # Try to find viable public subnet -- where internet gateway is in VPC and NAT gateway in subnet
    viableSubnetsList = []
    for natG in natGateResp['NatGateways']:
        for intG in intGateResp['InternetGateways']:
            viableSubnets = {}
            if natG['VpcId'] == intG['Attachments'][0]['VpcId']:
                viableSubnets['SubnetId'] = natG['SubnetId']
                viableSubnets['InternetGatewayId'] = intG['InternetGatewayId']
                viableSubnets['NatGatewayId'] = natG['NatGatewayId']
                viableSubnets['VpcId'] = natG['VpcId']
                viableSubnetsList.append(viableSubnets)
                
    logger.info(f'|VPC|{str(len(viableSubnetsList))} viable public subnets found|')
    
    return viableSubnetsList
    

def getViableSubnetRouteList(viableSubnetsList):
    # Make sure public routing correct to public subnet
    viableSubnetsRouteList = []
    for subnet in viableSubnetsList:
        subnetRouteResp = ec2.describe_route_tables(Filters= [{'Name': 'association.subnet-id', 'Values': [subnet['SubnetId']]}])
        match = 0
        for route in subnetRouteResp['RouteTables']:
            # check in route has internet gateway going to 0.0.0.0 and local route
            for row in route['Routes']:
                if row['GatewayId'] == subnet['InternetGatewayId'] and row['DestinationCidrBlock'] == '0.0.0.0/0':
                    match += 1
                elif row['GatewayId'] == 'local':
                    match += 1
            if match == 2:
                viableSubnetsRouteList.append(subnet)
            match = 0
    logger.info(f'|VPC|{str(len(viableSubnetsRouteList))} viable public subnets with correct routes found|')
    
    return viableSubnetsRouteList
    
    
def createPrivateSubnet(viableSubnetsRouteList):
    # Check if SageMakerSubnetPrivado already exists
    subnetResp = ec2.describe_subnets(
        Filters=[
            {
                'Name': 'vpc-id',
                'Values': [ viableSubnetsRouteList[0]['VpcId'] ]
            },
            {
                'Name': 'tag:Name',
                'Values': [ 'SageMakerSubnetPrivado' ]
            }
        ]
    )
    
    # Create new private subnet & route table if doesn't exist
    if not subnetResp['Subnets']:
        logger.info(f'|VPC|Private subnet SageMakerPrivado does not exits. Attempting to create...|')
        # -- Get VPC info to create new cidrBlock
        VPCResp = ec2.describe_vpcs(VpcIds=[viableSubnetsRouteList[0]['VpcId']])
        VPCCidrBlock = VPCResp['Vpcs'][0]['CidrBlock']
        cidrList = re.split('[. /]', VPCCidrBlock)
        
        newSubnetCidr = cidrList[0] + '.' + cidrList[1] + '.' + str(randint(50, 200)) + '.' + cidrList[4] + '/' + '20'
        try:
            subnetResp = ec2.create_subnet(TagSpecifications=[
                    {
                        'ResourceType': 'subnet',
                        'Tags': [
                            {
                                'Key': 'Name',
                                'Value': 'SageMakerSubnetPrivado'
                            }
                        ]
                    }
                ],
                CidrBlock=newSubnetCidr, 
                VpcId=viableSubnetsRouteList[0]['VpcId'])
            logger.info(f'|VPC|Private subnet SageMakerPrivado created|')
        except ClientError as e:
            logger.info(f'|VPC|Error f{e.response}|')
            # if e.response['Error']['Code'] == 'InvalidGroup.NotFound':
            #     pass
                
    logger.info(f'|VPC|Private subnet SageMakerPrivado already exists|')
            
    return subnetResp


def createRouteTable(viableSubnetsRouteList):
    # Create route table
    routeTableResp = ec2.create_route_table(VpcId=viableSubnetsRouteList[0]['VpcId'])
    
    # Create route for route table - NAT Gateway
    resp = ec2.create_route(
        DestinationCidrBlock='0.0.0.0/0',
        NatGatewayId=viableSubnetsRouteList[0]['NatGatewayId'],
        RouteTableId=routeTableResp['RouteTable']['RouteTableId'])
        
    logger.info('|VPC|Private route table created and NAT gateway route attached|')
    
    
def createSecurityGroup(viableSubnetsRouteList):
    # Create security group if SageMakerSecurityGroup doesn't exist
    try:
        secGroupResp = ec2.describe_security_groups(
            Filters=[
                {
                    'Name': 'vpc-id',
                    'Values': [ viableSubnetsRouteList[0]['VpcId'] ]
                },
                {
                    'Name': 'group-name',
                    'Values': [ 'SageMakerSecurityGroup' ]
                }
            ]
        )
        # If no match is found create
        if not secGroupResp['SecurityGroups']:
            logger.info('|VPC|Security group SageMakerSecurityGroup does not exits. Attempting to create...|')
            secGroupResp = ec2.create_security_group(Description='Security Group for Sagemaker Notebooks',
                GroupName='SageMakerSecurityGroup',
                VpcId=viableSubnetsRouteList[0]['VpcId'])
            logger.info('Security group SageMakerSecurityGroup created|')
            
            secGroupResp = ec2.describe_security_groups(
                Filters=[
                    {
                        'Name': 'vpc-id',
                        'Values': [ viableSubnetsRouteList[0]['VpcId'] ]
                    },
                    {
                        'Name': 'group-name',
                        'Values': [ 'SageMakerSecurityGroup' ]
                    }
                ]
            )
    except ClientError as e:
        if e.response['Error']['Code'] == 'InvalidGroup.NotFound':
            logger.info('|VPC|Security group SageMakerSecurityGroup does not exits. Attempting to create...|')
            secGroupResp = ec2.create_security_group(Description='Security Group for Sagemaker Notebooks',
                GroupName='SageMakerSecurityGroup',
                VpcId=viableSubnetsRouteList[0]['VpcId'])
            logger.info('Security group SageMakerSecurityGroup created|')
            
            secGroupResp = ec2.describe_security_groups(
                Filters=[
                    {
                        'Name': 'vpc-id',
                        'Values': [ viableSubnetsRouteList[0]['VpcId'] ]
                    },
                    {
                        'Name': 'group-name',
                        'Values': [ 'SageMakerSecurityGroup' ]
                    }
                ]
            )
            
    logger.info('|VPC|Security group SageMakerSecurityGroup already exists|')
    
    return secGroupResp
            
            
def createSageMakerNotebook(notebookInstanceName, subnetResp, secGroupResp, notebookDesc):
    # Create new notebook inside VPC
    response = sgm.create_notebook_instance(
        NotebookInstanceName=str(notebookInstanceName + 'InsideVPC'),
        InstanceType=notebookDesc['InstanceType'],
        SubnetId=subnetResp['Subnets'][0]['SubnetId'],
        SecurityGroupIds=[
            secGroupResp['SecurityGroups'][0]['GroupId']
        ],
        RoleArn=notebookDesc['RoleArn'],
        DirectInternetAccess='Disabled',
        VolumeSizeInGB=notebookDesc['VolumeSizeInGB'],
        RootAccess=notebookDesc['RootAccess'],
        PlatformIdentifier=notebookDesc['PlatformIdentifier']
    )
    logger.info(f'|SageMaker|New notebook {str(notebookInstanceName + "InsideVPC")} created|')

    
def lambda_handler(event, context):
    # Get notebook name from event
    notebookInstanceName = event['detail']['requestParameters']['notebookInstanceName']
    logger.info(f'|SageMaker|f{notebookInstanceName} exposed directly to internet. Attempting to create inside VPC...|')

    # Stop notebook
    notebookDesc = stopNotebook(notebookInstanceName)
    
    # Get details on existing notebook (only if not already retrieved)
    if not notebookDesc:
        notebookDesc = sgm.describe_notebook_instance(NotebookInstanceName=notebookInstanceName)
    
    # Check for viable public subnets
    viableSubnetsList = getViableSubnet()
    
    # Check that viable public subnets have viable routes to the internet via route table
    viableSubnetsRouteList = getViableSubnetRouteList(viableSubnetsList)
    
    # If no viable subnet, create new internet gateway, nat gateway, and public subnet
    
    # Create private subnet if SageMaker private subnet has not already been created via this script
    subnetResp = createPrivateSubnet(viableSubnetsRouteList)
    
    # Create private subnet route table
    createRouteTable(viableSubnetsRouteList)
    
    # Create SageMaker security
    secGroupResp = createSecurityGroup(viableSubnetsRouteList)
    
    # Create new notebook inside VPC
    createSageMakerNotebook(notebookInstanceName, subnetResp, secGroupResp, notebookDesc)
    
    # Check if new notebook was successfully created
    deleteExistingNotebook = True
    try:
        notebookDesc = sgm.describe_notebook_instance(NotebookInstanceName=str(notebookInstanceName + 'InsideVPC'))
    except ClientError as e:
        if e.response['Error']['Message'] == 'RecordNotFound':
            deleteExistingNotebook = False
            logger.info(f'|SageMaker|Error occured when attempting to create new notebook. Old notebook will not be deleted|')

    # Delete non VPC notebook if new notebook was successfully created
    if deleteExistingNotebook:
        sgm.delete_notebook_instance(NotebookInstanceName=notebookInstanceName)
  