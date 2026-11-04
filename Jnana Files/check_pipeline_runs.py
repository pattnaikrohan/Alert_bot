from azure.identity import DeviceCodeCredential  # no local server needed
from azure.mgmt.datafactory import DataFactoryManagementClient
from datetime import datetime, timedelta

# --- Fill these in ---
# SUBSCRIPTION_ID must be the GUID, not the subscription's display name.
# Get it with: az account show --subscription "PSABDP-CSDW-NonProd" --query id -o tsv
SUBSCRIPTION_ID = "b10dba21-0331-4510-a7cb-924077c9cb52"
RESOURCE_GROUP = "dev-csdw-rg"
FACTORY_NAME = "dev-csdw-data-factory"
PIPELINE_NAME = "DEV-SG-TERMINAL-CSV"

# Interactive login as YOURSELF — prints a code in the terminal; open
# https://microsoft.com/devicelogin in any browser and enter it there.
# (Avoids InteractiveBrowserCredential's localhost redirect, which your
# firewall/proxy may be blocking — that's the ERR_CONNECTION_REFUSED you saw.)
credential = DeviceCodeCredential()

client = DataFactoryManagementClient(credential, SUBSCRIPTION_ID)

# Query pipeline runs in the last 7 days, filtered to this pipeline
filter_params = {
    "lastUpdatedAfter": datetime.utcnow() - timedelta(days=7),
    "lastUpdatedBefore": datetime.utcnow(),
    "filters": [
        {
            "operand": "PipelineName",
            "operator": "Equals",
            "values": [PIPELINE_NAME],
        }
    ],
}

runs = client.pipeline_runs.query_by_factory(
    resource_group_name=RESOURCE_GROUP,
    factory_name=FACTORY_NAME,
    filter_parameters=filter_params,
)

for run in runs.value:
    print(f"RunId: {run.run_id}")
    print(f"  Pipeline: {run.pipeline_name}")
    print(f"  Status: {run.status}")
    print(f"  Start: {run.run_start}")
    print(f"  End: {run.run_end}")
    print(f"  Duration (ms): {run.duration_in_ms}")
    if run.status == "Failed":
        print(f"  Message: {run.message}")
    print()
