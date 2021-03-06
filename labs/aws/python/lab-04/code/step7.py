from pulumi import export, Output, ResourceOptions
import pulumi_aws as aws
import json, hashlib
from pulumi_kubernetes import Provider
from pulumi_kubernetes.apps.v1 import Deployment
from pulumi_kubernetes.core.v1 import Service, Namespace

h = hashlib.new('sha1')

# Create the EKS Service Role and the correct role attachments
service_role = aws.iam.Role("eks-service-role",
    assume_role_policy=json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "",
            "Effect": "Allow",
            "Principal": {
                "Service": "eks.amazonaws.com"
            },
            "Action": "sts:AssumeRole"
        }]
    })
)

service_role_managed_policy_arns = [
    "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
    "arn:aws:iam::aws:policy/AmazonEKSServicePolicy"
]

for policy in service_role_managed_policy_arns:
    h.update(policy.encode('utf-8'))
    role_policy_attachment = aws.iam.RolePolicyAttachment(f"eks-service-role-{h.hexdigest()[0:8]}",
        policy_arn=policy,
        role=service_role.name
    )

# Create the EKS NodeGroup Role and the correct role attachments
node_group_role = aws.iam.Role("eks-nodegroup-role",
    assume_role_policy=json.dumps({
       "Version": "2012-10-17",
       "Statement": [{
           "Sid": "",
           "Effect": "Allow",
           "Principal": {
               "Service": "ec2.amazonaws.com"
           },
           "Action": "sts:AssumeRole"
       }]
    })
)

nodegroup_role_managed_policy_arns = [
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
]

for policy in nodegroup_role_managed_policy_arns:
    h.update(policy.encode('utf-8'))
    role_policy_attachment = aws.iam.RolePolicyAttachment(f"eks-nodegroup-role-{h.hexdigest()[0:8]}",
        policy_arn=policy,
        role=node_group_role.name
    )

# Get the VPC and subnets to launch the EKS cluster into
default_vpc = aws.ec2.get_vpc(default="true")
default_vpc_subnets = aws.ec2.get_subnet_ids(vpc_id=default_vpc.id)

# Create the Security Group that allows access to the cluster pods
sg = aws.ec2.SecurityGroup("eks-cluster-security-group",
    vpc_id=default_vpc.id,
    revoke_rules_on_delete="true",
    ingress=[{
       'cidr_blocks' : ["0.0.0.0/0"],
       'from_port' : '80',
       'to_port' : '80',
       'protocol' : 'tcp',
    }]
)

sg_rule = aws.ec2.SecurityGroupRule("eks-cluster-security-group-egress-rule",
    type="egress",
    from_port=0,
    to_port=0,
    protocol="-1",
    cidr_blocks=["0.0.0.0/0"],
    security_group_id=sg.id
)

# Create EKS Cluster
cluster = aws.eks.Cluster("eks-cluster",
    role_arn=service_role.arn,
    vpc_config={
      "security_group_ids": [sg.id],
      "subnet_ids": default_vpc_subnets.ids,
      "endpointPrivateAccess": "false",
      "endpointPublicAccess": "true",
      "publicAccessCidrs": ["0.0.0.0/0"],
    },
)

# Create Cluster NodeGroup
node_group = aws.eks.NodeGroup("eks-node-group",
    cluster_name=cluster.name,
    node_role_arn=node_group_role.arn,
    subnet_ids=default_vpc_subnets.ids,
    scaling_config = {
       "desired_size": 2,
       "max_size": 2,
       "min_size": 1,
    },
)

def generateKubeconfig(endpoint, cert_data, cluster_name):
    return json.dumps({
        "apiVersion": "v1",
        "clusters": [{
            "cluster": {
                "server": f"{endpoint}",
                "certificate-authority-data": f"{cert_data}"
            },
            "name": "kubernetes",
        }],
        "contexts": [{
            "context": {
                "cluster": "kubernetes",
                "user": "aws",
            },
            "name": "aws",
        }],
        "current-context": "aws",
        "kind": "Config",
        "users": [{
            "name": "aws",
            "user": {
                "exec": {
                    "apiVersion": "client.authentication.k8s.io/v1alpha1",
                    "command": "aws-iam-authenticator",
                    "args": [
                        "token",
                        "-i",
                        f"{cluster_name}",
                    ],
                },
            },
        }],
    })

# Create the KubeConfig Structure as per https://docs.aws.amazon.com/eks/latest/userguide/create-kubeconfig.html
kubeconfig = Output.all(cluster.endpoint, cluster.certificate_authority["data"], cluster.name).apply(lambda args: generateKubeconfig(args[0], args[1], args[2]))

# Declare a provider using the KubeConfig we created
# This will be used to interact with the EKS cluster
k8s_provider = Provider("k8s-provider", kubeconfig=kubeconfig)

# Create a Namespace object https://kubernetes.io/docs/concepts/overview/working-with-objects/namespaces/
ns = Namespace("app-ns",
    metadata={
       "name": "joe-duffy",
    },
    opts=ResourceOptions(provider=k8s_provider)
)

app_labels = {
    "app": "iac-workshop"
}
app_deployment = Deployment("app-dep",
    metadata={
        "namespace": ns.metadata["name"]
    },
    spec={
        "selector": {
            "match_labels": app_labels,
        },
        "replicas": 3,
        "template": {
            "metadata": {
                "labels": app_labels,
            },
            "spec": {
                "containers": [{
                    "name": "iac-workshop",
                    "image": "jocatalin/kubernetes-bootcamp:v2",
                }],
            },
        },
    },
    opts=ResourceOptions(provider=k8s_provider)
)

service = Service("app-service",
    metadata={
      "namespace": ns.metadata["name"],
      "labels": app_labels
    },
    spec={
      "ports": [{
          "port": 80,
          "target_port": 8080,
      }],
      "selector": app_labels,
      "type": "LoadBalancer",
    },
    opts=ResourceOptions(provider=k8s_provider)
)

export('url', Output.all(service.status['load_balancer']['ingress'][0]['hostname'], service.spec['ports'][0]['port']) \
       .apply(lambda args: f"http://{args[0]}:{round(args[1])}"))
