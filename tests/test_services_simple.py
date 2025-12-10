import pytest
import json
from unittest.mock import patch


class TestServicesTemplateSimple:
    """Simplified test cases for the Services CloudFormation template that avoid complex template generation"""

    def test_load_service_config_function(self):
        """Test that the load_service_config function exists and is importable"""
        from lakerunner_services import load_service_config
        assert callable(load_service_config)

    def test_create_services_template_function(self):
        """Test that the create_services_template function exists and is importable"""
        from lakerunner_services import create_services_template
        assert callable(create_services_template)

    @patch('lakerunner_services.load_service_config')
    def test_template_generation_basic(self, mock_load_config):
        """Test basic template generation with minimal mock config"""
        # Minimal config that should not cause errors
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "go_services": "test:latest",
                "query_api": "test:latest", 
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }
        
        from lakerunner_services import create_services_template
        
        # This should not raise an exception
        template = create_services_template()
        assert template is not None

    @patch('lakerunner_services.load_service_config')
    def test_template_has_basic_structure(self, mock_load_config):
        """Test that generated template has basic CloudFormation structure"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "go_services": "test:latest",
                "query_api": "test:latest", 
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        # Check basic CloudFormation structure
        assert "Description" in template_dict
        assert "Parameters" in template_dict
        assert "Resources" in template_dict

    @patch('lakerunner_services.load_service_config')
    def test_template_description_correct(self, mock_load_config):
        """Test that template description is correct"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "go_services": "test:latest",
                "query_api": "test:latest", 
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        expected_desc = "Lakerunner Services: ECS services, task definitions, IAM roles, and ALB integration"
        assert template_dict["Description"] == expected_desc

    @patch('lakerunner_services.load_service_config')
    def test_commoninfra_parameter_exists(self, mock_load_config):
        """Test that CommonInfraStackName parameter exists"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "go_services": "test:latest",
                "query_api": "test:latest", 
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        # Should have CommonInfraStackName parameter for imports
        assert "CommonInfraStackName" in template_dict["Parameters"]

    @patch('lakerunner_services.load_service_config')
    def test_image_parameters_exist(self, mock_load_config):
        """Test that container image parameters exist"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "go_services": "test:latest",
                "query_api": "test:latest",
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())

        parameters = template_dict["Parameters"]

        # Should have image override parameters (Grafana moved to separate stack)
        # All services now use unified GoServicesImage parameter
        expected_image_params = ["GoServicesImage"]
        for param in expected_image_params:
            assert param in parameters, f"Image parameter {param} not found"

    @patch('lakerunner_services.load_service_config')
    def test_signal_type_parameters_exist(self, mock_load_config):
        """Test that signal type parameters exist with correct defaults"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "go_services": "test:latest",
                "query_api": "test:latest",
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())

        parameters = template_dict["Parameters"]

        # Check that all signal type parameters exist
        assert "EnableLogs" in parameters, "EnableLogs parameter not found"
        assert "EnableMetrics" in parameters, "EnableMetrics parameter not found"
        assert "EnableTraces" in parameters, "EnableTraces parameter not found"

        # Check parameter types and allowed values
        for param_name in ["EnableLogs", "EnableMetrics", "EnableTraces"]:
            param = parameters[param_name]
            assert param["Type"] == "String", f"{param_name} should be String type"
            assert param["AllowedValues"] == ["Yes", "No"], f"{param_name} should have Yes/No values"

        # Check defaults
        assert parameters["EnableLogs"]["Default"] == "Yes", "EnableLogs should default to Yes"
        assert parameters["EnableMetrics"]["Default"] == "Yes", "EnableMetrics should default to Yes"
        assert parameters["EnableTraces"]["Default"] == "No", "EnableTraces should default to No"

    @patch('lakerunner_services.load_service_config')
    def test_signal_type_conditions_exist(self, mock_load_config):
        """Test that signal type conditions exist"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "go_services": "test:latest",
                "query_api": "test:latest",
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())

        conditions = template_dict.get("Conditions", {})

        # Check that all signal type conditions exist
        assert "CreateLogsServices" in conditions, "CreateLogsServices condition not found"
        assert "CreateMetricsServices" in conditions, "CreateMetricsServices condition not found"
        assert "CreateTracesServices" in conditions, "CreateTracesServices condition not found"

    @patch('lakerunner_services.load_service_config')
    def test_signal_type_service_creation(self, mock_load_config):
        """Test that services are conditionally created based on signal type"""
        mock_load_config.return_value = {
            "services": {
                "test-logs-service": {
                    "signal_type": "logs",
                    "command": ["/app/bin/lakerunner", "test"],
                    "cpu": 512,
                    "memory_mib": 1024,
                    "replicas": 1,
                    "health_check": {
                        "type": "go",
                        "command": ["/app/bin/lakerunner", "sysinfo"]
                    },
                    "environment": {}
                },
                "test-metrics-service": {
                    "signal_type": "metrics",
                    "command": ["/app/bin/lakerunner", "test"],
                    "cpu": 512,
                    "memory_mib": 1024,
                    "replicas": 1,
                    "health_check": {
                        "type": "go",
                        "command": ["/app/bin/lakerunner", "sysinfo"]
                    },
                    "environment": {}
                },
                "test-traces-service": {
                    "signal_type": "traces",
                    "command": ["/app/bin/lakerunner", "test"],
                    "cpu": 512,
                    "memory_mib": 1024,
                    "replicas": 1,
                    "health_check": {
                        "type": "go",
                        "command": ["/app/bin/lakerunner", "sysinfo"]
                    },
                    "environment": {}
                },
                "test-common-service": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "test"],
                    "cpu": 512,
                    "memory_mib": 1024,
                    "replicas": 1,
                    "health_check": {
                        "type": "go",
                        "command": ["/app/bin/lakerunner", "sysinfo"]
                    },
                    "environment": {}
                }
            },
            "images": {
                "go_services": "test:latest"
            }
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())

        resources = template_dict.get("Resources", {})

        # Check that services with signal types have conditions
        # LogGroups
        assert resources["LogGroupTestLogsService"].get("Condition") == "CreateLogsServices"
        assert resources["LogGroupTestMetricsService"].get("Condition") == "CreateMetricsServices"
        assert resources["LogGroupTestTracesService"].get("Condition") == "CreateTracesServices"
        assert "Condition" not in resources["LogGroupTestCommonService"]

        # TaskDefinitions
        assert resources["TaskDefTestLogsService"].get("Condition") == "CreateLogsServices"
        assert resources["TaskDefTestMetricsService"].get("Condition") == "CreateMetricsServices"
        assert resources["TaskDefTestTracesService"].get("Condition") == "CreateTracesServices"
        assert "Condition" not in resources["TaskDefTestCommonService"]

        # Services
        assert resources["ServiceTestLogsService"].get("Condition") == "CreateLogsServices"
        assert resources["ServiceTestMetricsService"].get("Condition") == "CreateMetricsServices"
        assert resources["ServiceTestTracesService"].get("Condition") == "CreateTracesServices"
        assert "Condition" not in resources["ServiceTestCommonService"]

        # Outputs
        outputs = template_dict.get("Outputs", {})
        assert outputs["ServiceTestLogsServiceArn"].get("Condition") == "CreateLogsServices"
        assert outputs["ServiceTestMetricsServiceArn"].get("Condition") == "CreateMetricsServices"
        assert outputs["ServiceTestTracesServiceArn"].get("Condition") == "CreateTracesServices"
        assert "Condition" not in outputs["ServiceTestCommonServiceArn"]

    @patch('lakerunner_services.load_service_config')
    def test_query_services_have_cpu_memory_replicas_params(self, mock_load_config):
        """Test that query-api and query-worker have CPU, Memory, and Replicas parameters"""
        mock_load_config.return_value = {
            "services": {
                "lakerunner-query-api": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "query-api"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 2,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-query-worker": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "query-worker"],
                    "cpu": 2048,
                    "memory_mib": 8192,
                    "replicas": 4,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                }
            },
            "images": {"go_services": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        parameters = template_dict["Parameters"]

        # Query API parameters
        assert "QueryApiReplicas" in parameters
        assert "QueryApiCpu" in parameters
        assert "QueryApiMemory" in parameters
        assert parameters["QueryApiReplicas"]["Default"] == "2"
        assert parameters["QueryApiCpu"]["Default"] == "1024"
        assert parameters["QueryApiMemory"]["Default"] == "2048"

        # Query Worker parameters
        assert "QueryWorkerReplicas" in parameters
        assert "QueryWorkerCpu" in parameters
        assert "QueryWorkerMemory" in parameters
        assert parameters["QueryWorkerReplicas"]["Default"] == "4"
        assert parameters["QueryWorkerCpu"]["Default"] == "2048"
        assert parameters["QueryWorkerMemory"]["Default"] == "8192"

    @patch('lakerunner_services.load_service_config')
    def test_worker_services_have_memory_replicas_params(self, mock_load_config):
        """Test that ingest/compact/rollup services have Memory and Replicas parameters (not CPU)"""
        mock_load_config.return_value = {
            "services": {
                "lakerunner-ingest-logs": {
                    "signal_type": "logs",
                    "command": ["/app/bin/lakerunner", "ingest-logs"],
                    "cpu": 1024,
                    "memory_mib": 4096,
                    "replicas": 4,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-compact-metrics": {
                    "signal_type": "metrics",
                    "command": ["/app/bin/lakerunner", "compact-metrics"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 2,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-rollup-metrics": {
                    "signal_type": "metrics",
                    "command": ["/app/bin/lakerunner", "rollup-metrics"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 2,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                }
            },
            "images": {"go_services": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        parameters = template_dict["Parameters"]

        # Ingest logs - has memory and replicas, not CPU
        assert "IngestLogsReplicas" in parameters
        assert "IngestLogsMemory" in parameters
        assert "IngestLogsCpu" not in parameters
        assert parameters["IngestLogsReplicas"]["Default"] == "4"
        assert parameters["IngestLogsMemory"]["Default"] == "4096"

        # Compact metrics - has memory and replicas, not CPU
        assert "CompactMetricsReplicas" in parameters
        assert "CompactMetricsMemory" in parameters
        assert "CompactMetricsCpu" not in parameters

        # Rollup metrics - has memory and replicas, not CPU
        assert "RollupMetricsReplicas" in parameters
        assert "RollupMetricsMemory" in parameters
        assert "RollupMetricsCpu" not in parameters

    @patch('lakerunner_services.load_service_config')
    def test_replicas_only_services_have_replicas_param_only(self, mock_load_config):
        """Test that pubsub and boxer have only Replicas parameter (no CPU/Memory)"""
        mock_load_config.return_value = {
            "services": {
                "lakerunner-pubsub-sqs": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "pubsub", "sqs"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-boxer-common": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "boxer", "--all"],
                    "cpu": 256,
                    "memory_mib": 512,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                }
            },
            "images": {"go_services": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        parameters = template_dict["Parameters"]

        # Pubsub SQS - has replicas only
        assert "PubsubSqsReplicas" in parameters
        assert "PubsubSqsMemory" not in parameters
        assert "PubsubSqsCpu" not in parameters

        # Boxer Common - has replicas only
        assert "BoxerCommonReplicas" in parameters
        assert "BoxerCommonMemory" not in parameters
        assert "BoxerCommonCpu" not in parameters

    @patch('lakerunner_services.load_service_config')
    def test_sweeper_monitor_have_no_params(self, mock_load_config):
        """Test that sweeper and monitoring use YAML values only (no parameters)"""
        mock_load_config.return_value = {
            "services": {
                "lakerunner-sweeper": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "sweeper"],
                    "cpu": 256,
                    "memory_mib": 512,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-monitoring": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "monitoring", "serve"],
                    "cpu": 256,
                    "memory_mib": 512,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                }
            },
            "images": {"go_services": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        parameters = template_dict["Parameters"]

        # Sweeper - no parameters
        assert "SweeperReplicas" not in parameters
        assert "SweeperMemory" not in parameters
        assert "SweeperCpu" not in parameters

        # Monitoring - no parameters
        assert "MonitoringReplicas" not in parameters
        assert "MonitoringMemory" not in parameters
        assert "MonitoringCpu" not in parameters

    @patch('lakerunner_services.load_service_config')
    def test_task_definitions_use_correct_values(self, mock_load_config):
        """Test that task definitions use Ref for params or hardcoded YAML values appropriately"""
        mock_load_config.return_value = {
            "services": {
                "lakerunner-query-api": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "query-api"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 2,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-ingest-logs": {
                    "signal_type": "logs",
                    "command": ["/app/bin/lakerunner", "ingest-logs"],
                    "cpu": 1024,
                    "memory_mib": 4096,
                    "replicas": 4,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-sweeper": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "sweeper"],
                    "cpu": 256,
                    "memory_mib": 512,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                }
            },
            "images": {"go_services": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]

        # Query API - CPU and Memory should be Ref
        query_api_task = resources["TaskDefLakerunnerQueryApi"]["Properties"]
        assert query_api_task["Cpu"] == {"Ref": "QueryApiCpu"}
        assert query_api_task["Memory"] == {"Ref": "QueryApiMemory"}

        # Ingest Logs - CPU should be hardcoded, Memory should be Ref
        ingest_logs_task = resources["TaskDefLakerunnerIngestLogs"]["Properties"]
        assert ingest_logs_task["Cpu"] == "1024"
        assert ingest_logs_task["Memory"] == {"Ref": "IngestLogsMemory"}

        # Sweeper - CPU and Memory should be hardcoded from YAML
        sweeper_task = resources["TaskDefLakerunnerSweeper"]["Properties"]
        assert sweeper_task["Cpu"] == "256"
        assert sweeper_task["Memory"] == "512"

    @patch('lakerunner_services.load_service_config')
    def test_ecs_services_use_correct_desired_count(self, mock_load_config):
        """Test that ECS services use Ref for replicas or hardcoded values appropriately"""
        mock_load_config.return_value = {
            "services": {
                "lakerunner-query-api": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "query-api"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 2,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-pubsub-sqs": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "pubsub", "sqs"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-sweeper": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "sweeper"],
                    "cpu": 256,
                    "memory_mib": 512,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                }
            },
            "images": {"go_services": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]

        # Query API - DesiredCount should be Ref
        query_api_svc = resources["ServiceLakerunnerQueryApi"]["Properties"]
        assert query_api_svc["DesiredCount"] == {"Ref": "QueryApiReplicas"}

        # Pubsub SQS - DesiredCount should be Ref (replicas-only service)
        pubsub_svc = resources["ServiceLakerunnerPubsubSqs"]["Properties"]
        assert pubsub_svc["DesiredCount"] == {"Ref": "PubsubSqsReplicas"}

        # Sweeper - DesiredCount should be hardcoded from YAML
        sweeper_svc = resources["ServiceLakerunnerSweeper"]["Properties"]
        assert sweeper_svc["DesiredCount"] == "1"

    @patch('lakerunner_services.load_service_config')
    def test_autoscaling_parameters_exist(self, mock_load_config):
        """Test that auto-scaling parameters exist with correct defaults"""
        mock_load_config.return_value = {
            "services": {},
            "images": {"go_services": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        parameters = template_dict["Parameters"]

        # Check auto-scaling parameters exist
        assert "EnableAutoScaling" in parameters
        assert "AutoScalingMaxReplicas" in parameters
        assert "AutoScalingCPUTarget" in parameters
        assert "AutoScalingScaleOutCooldown" in parameters
        assert "AutoScalingScaleInCooldown" in parameters

        # Check defaults
        assert parameters["EnableAutoScaling"]["Default"] == "No"
        assert parameters["AutoScalingMaxReplicas"]["Default"] == "10"
        assert parameters["AutoScalingCPUTarget"]["Default"] == "70"
        assert parameters["AutoScalingScaleOutCooldown"]["Default"] == "60"
        assert parameters["AutoScalingScaleInCooldown"]["Default"] == "300"

        # Check EnableAutoScaling is Yes/No
        assert parameters["EnableAutoScaling"]["AllowedValues"] == ["Yes", "No"]

    @patch('lakerunner_services.load_service_config')
    def test_autoscaling_conditions_exist(self, mock_load_config):
        """Test that auto-scaling conditions exist"""
        mock_load_config.return_value = {
            "services": {},
            "images": {"go_services": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        conditions = template_dict.get("Conditions", {})

        # Check auto-scaling conditions exist
        assert "AutoScalingEnabled" in conditions
        assert "AutoScaleLogsServices" in conditions
        assert "AutoScaleMetricsServices" in conditions
        assert "AutoScaleTracesServices" in conditions

    @patch('lakerunner_services.load_service_config')
    def test_autoscaling_resources_created_for_worker_services(self, mock_load_config):
        """Test that ScalableTarget and ScalingPolicy are created for ingest/compact/rollup services"""
        mock_load_config.return_value = {
            "services": {
                "lakerunner-ingest-logs": {
                    "signal_type": "logs",
                    "command": ["/app/bin/lakerunner", "ingest-logs"],
                    "cpu": 1024,
                    "memory_mib": 4096,
                    "replicas": 4,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-compact-metrics": {
                    "signal_type": "metrics",
                    "command": ["/app/bin/lakerunner", "compact-metrics"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 2,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-rollup-metrics": {
                    "signal_type": "metrics",
                    "command": ["/app/bin/lakerunner", "rollup-metrics"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 2,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-ingest-traces": {
                    "signal_type": "traces",
                    "command": ["/app/bin/lakerunner", "ingest-traces"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                }
            },
            "images": {"go_services": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]

        # Check ScalableTarget resources exist with correct conditions
        assert "ScalableTargetLakerunnerIngestLogs" in resources
        assert resources["ScalableTargetLakerunnerIngestLogs"]["Condition"] == "AutoScaleLogsServices"

        assert "ScalableTargetLakerunnerCompactMetrics" in resources
        assert resources["ScalableTargetLakerunnerCompactMetrics"]["Condition"] == "AutoScaleMetricsServices"

        assert "ScalableTargetLakerunnerRollupMetrics" in resources
        assert resources["ScalableTargetLakerunnerRollupMetrics"]["Condition"] == "AutoScaleMetricsServices"

        assert "ScalableTargetLakerunnerIngestTraces" in resources
        assert resources["ScalableTargetLakerunnerIngestTraces"]["Condition"] == "AutoScaleTracesServices"

        # Check ScalingPolicy resources exist with correct conditions
        assert "ScalingPolicyLakerunnerIngestLogs" in resources
        assert resources["ScalingPolicyLakerunnerIngestLogs"]["Condition"] == "AutoScaleLogsServices"

        assert "ScalingPolicyLakerunnerCompactMetrics" in resources
        assert resources["ScalingPolicyLakerunnerCompactMetrics"]["Condition"] == "AutoScaleMetricsServices"

    @patch('lakerunner_services.load_service_config')
    def test_autoscaling_not_created_for_non_worker_services(self, mock_load_config):
        """Test that auto-scaling is NOT created for query, pubsub, boxer, sweeper, monitoring"""
        mock_load_config.return_value = {
            "services": {
                "lakerunner-query-api": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "query-api"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 2,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-pubsub-sqs": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "pubsub", "sqs"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-sweeper": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "sweeper"],
                    "cpu": 256,
                    "memory_mib": 512,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                }
            },
            "images": {"go_services": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]

        # No auto-scaling resources for non-worker services
        assert "ScalableTargetLakerunnerQueryApi" not in resources
        assert "ScalingPolicyLakerunnerQueryApi" not in resources
        assert "ScalableTargetLakerunnerPubsubSqs" not in resources
        assert "ScalingPolicyLakerunnerPubsubSqs" not in resources
        assert "ScalableTargetLakerunnerSweeper" not in resources
        assert "ScalingPolicyLakerunnerSweeper" not in resources

    @patch('lakerunner_services.load_service_config')
    def test_autoscaling_policy_uses_cpu_target_tracking(self, mock_load_config):
        """Test that scaling policy uses CPU target tracking with correct configuration"""
        mock_load_config.return_value = {
            "services": {
                "lakerunner-ingest-logs": {
                    "signal_type": "logs",
                    "command": ["/app/bin/lakerunner", "ingest-logs"],
                    "cpu": 1024,
                    "memory_mib": 4096,
                    "replicas": 4,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                }
            },
            "images": {"go_services": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]

        # Check scaling policy configuration
        policy = resources["ScalingPolicyLakerunnerIngestLogs"]["Properties"]
        assert policy["PolicyType"] == "TargetTrackingScaling"

        config = policy["TargetTrackingScalingPolicyConfiguration"]
        assert config["TargetValue"] == {"Ref": "AutoScalingCPUTarget"}
        assert config["ScaleInCooldown"] == {"Ref": "AutoScalingScaleInCooldown"}
        assert config["ScaleOutCooldown"] == {"Ref": "AutoScalingScaleOutCooldown"}
        assert config["PredefinedMetricSpecification"]["PredefinedMetricType"] == "ECSServiceAverageCPUUtilization"

    @patch('lakerunner_services.load_service_config')
    def test_scalable_target_uses_correct_min_max(self, mock_load_config):
        """Test that ScalableTarget uses replicas param for min and max param for max"""
        mock_load_config.return_value = {
            "services": {
                "lakerunner-ingest-logs": {
                    "signal_type": "logs",
                    "command": ["/app/bin/lakerunner", "ingest-logs"],
                    "cpu": 1024,
                    "memory_mib": 4096,
                    "replicas": 4,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                }
            },
            "images": {"go_services": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]

        # Check scalable target configuration
        target = resources["ScalableTargetLakerunnerIngestLogs"]["Properties"]
        assert target["MinCapacity"] == {"Ref": "IngestLogsReplicas"}
        assert target["MaxCapacity"] == {"Ref": "AutoScalingMaxReplicas"}
        assert target["ScalableDimension"] == "ecs:service:DesiredCount"
        assert target["ServiceNamespace"] == "ecs"