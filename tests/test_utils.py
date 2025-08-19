"""Test utilities for CloudFormation template tests"""
import sys
import os

def setup_ecs_path():
    """Add ECS source path to sys.path for imports"""
    ecs_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'ecs')
    if ecs_path not in sys.path:
        sys.path.insert(0, ecs_path)

def setup_eks_path():
    """Add EKS source path to sys.path for imports"""
    eks_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'eks')
    if eks_path not in sys.path:
        sys.path.insert(0, eks_path)