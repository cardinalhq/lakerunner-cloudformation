#!/usr/bin/env python3
# Copyright (C) 2026 CardinalHQ, Inc
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import os
import yaml

from troposphere import Equals, Output, Parameter, Ref, Template


def load_maestro_config(config_file="lakerunner-maestro-defaults.yaml"):
    """Load default configuration for the Maestro stack from YAML."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", config_file)
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def create_maestro_template():
    """Create the CloudFormation template for the Maestro + MCP Gateway stack."""
    t = Template()
    t.set_description(
        "Lakerunner Maestro + MCP Gateway: single ECS Fargate service with a"
        " stack-local ALB. Reuses CommonInfra RDS and runs a psql init"
        " container that creates the maestro DB and user."
    )
    t.set_metadata({
        "AWS::CloudFormation::Interface": {
            "ParameterGroups": [],
            "ParameterLabels": {},
        }
    })
    _placeholder_param = t.add_parameter(Parameter(
        "CommonInfraStackName", Type="String",
        Description="REQUIRED: Name of the CommonInfra stack to import values from."
    ))
    t.add_condition("PlaceholderAlwaysFalse", Equals(Ref(_placeholder_param), "__never__"))
    t.add_output(Output("PlaceholderOutput", Value=Ref(_placeholder_param)))
    return t


if __name__ == "__main__":
    template = create_maestro_template()
    print(template.to_yaml())
