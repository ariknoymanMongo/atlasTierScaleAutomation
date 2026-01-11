#!/usr/bin/env python3
"""
Scale Up All Clusters

This script scales up all clusters (all shards) from baseTier to scaleUpTier.
No validation checks - simple scale-up operation.

Use Case: Scheduled scale-up in the morning

Requirements:
    pip install requests python-dotenv

Usage:
    python scale_up_all.py --project-id PROJECT_ID --public-key KEY --private-key KEY
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import requests
from requests.auth import HTTPDigestAuth
from dotenv import load_dotenv

load_dotenv()

DEFAULT_NODE_COUNT = 3


def get_cluster_details(project_id: str, cluster_name: str, public_key: str, private_key: str) -> Optional[Dict]:
    """Get cluster details using v2 API"""
    try:
        url = f"https://cloud.mongodb.com/api/atlas/v2/groups/{project_id}/clusters/{cluster_name}"
        headers = {"Accept": "application/vnd.atlas.2024-10-23+json"}
        auth = HTTPDigestAuth(public_key, private_key)
        response = requests.get(url, headers=headers, auth=auth)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error getting cluster details: {e}")
        return None


def get_shard_tier(cluster_info: Dict, shard_index: int) -> Optional[str]:
    """Get current tier for a specific shard"""
    replication_specs = cluster_info.get("replicationSpecs", [])
    if shard_index < 0 or shard_index >= len(replication_specs):
        return None
    
    spec = replication_specs[shard_index]
    region_configs = spec.get("regionConfigs", [])
    if not region_configs and spec.get("regionsConfig"):
        regions_obj = spec.get("regionsConfig", {})
        if regions_obj:
            region_configs = [list(regions_obj.values())[0]]
    
    if region_configs:
        rc0 = region_configs[0]
        effective_specs = rc0.get("effectiveElectableSpecs", {})
        return effective_specs.get("instanceSize")
    
    return None


def update_config_timestamp(config_path: str, cluster_name: str, shard_indices: List[int]):
    """Update lastTierUpdate timestamp for scaled-up shards"""
    try:
        with open(config_path, 'r') as f:
            data = json.load(f)
        
        now_iso = datetime.now(timezone.utc).isoformat()
        for entry in data:
            if entry.get('clusterName') == cluster_name:
                for shard in entry.get('shards', []):
                    if shard.get('shardIndex') in shard_indices:
                        shard['lastTierUpdate'] = now_iso
        
        with open(config_path, 'w') as f:
            json.dump(data, f, indent=2)
        
        print(f"  Updated lastTierUpdate for {cluster_name} shard(s) {shard_indices}")
    except Exception as e:
        print(f"  Warning: Could not update config file: {e}")


def scale_up_cluster(project_id: str, cluster_name: str, base_tier: str, scale_up_tier: str,
                     shard_indices: List[int], public_key: str, private_key: str) -> Tuple[bool, List[int]]:
    """Scale up all specified shards in a cluster from baseTier to scaleUpTier"""
    print(f"\n{'='*60}")
    print(f"Scaling up cluster: {cluster_name}")
    print(f"  baseTier: {base_tier}, scaleUpTier: {scale_up_tier}")
    print(f"  Shards to scale: {shard_indices}")
    print(f"{'='*60}")
    
    # Get cluster details
    cluster_info = get_cluster_details(project_id, cluster_name, public_key, private_key)
    if not cluster_info:
        print(f"✗ Error: Could not get cluster details for {cluster_name}")
        return False
    
    replication_specs = cluster_info.get("replicationSpecs", [])
    if not replication_specs:
        print(f"✗ Error: No replicationSpecs found for {cluster_name}")
        return False
    
    # Check which shards need scaling up
    shards_to_scale = []
    for shard_index in shard_indices:
        current_tier = get_shard_tier(cluster_info, shard_index)
        if not current_tier:
            print(f"  ⚠ Warning: Could not determine tier for shard[{shard_index}] - skipping")
            continue
        
        if current_tier == base_tier:
            print(f"  ✓ Shard[{shard_index}] is at {base_tier} - will scale up to {scale_up_tier}")
            shards_to_scale.append(shard_index)
        elif current_tier == scale_up_tier:
            print(f"  ⊙ Shard[{shard_index}] is already at {scale_up_tier} - skipping")
        else:
            print(f"  ⚠ Warning: Shard[{shard_index}] is at {current_tier} (not {base_tier} or {scale_up_tier}) - skipping")
    
    if not shards_to_scale:
        print(f"\n✓ No shards need scaling up for {cluster_name}")
        return True, []
    
    # Prepare update payload
    import copy
    update_payload = copy.deepcopy(cluster_info)
    
    # Remove read-only fields
    read_only_fields = [
        "id", "mongoURI", "connectionStrings", "stateName", "createDate", "updateDate",
        "links", "groupId", "replicationSpec", "mongoURIUpdated", "mongoURIWithOptions",
        "srvAddress", "mongoDBVersion", "numShards", "name", "mongoDBMajorVersion",
        "providerBackupEnabled", "pitEnabled", "backupEnabled", "clusterType",
        "replicationFactor", "rootCertType", "terminationProtectionEnabled",
        "versionReleaseSystem", "diskWarmingMode", "encryptionAtRestProvider",
        "globalClusterSelfManagedSharding", "labels", "biConnector",
        "customOpensslCipherConfigTls13", "minimumEnabledTlsProtocol", "tlsCipherConfigMode"
    ]
    for field in read_only_fields:
        update_payload.pop(field, None)
    
    replication_specs_update = update_payload.get("replicationSpecs", [])
    original_count = len(replication_specs_update)
    
    if len(replication_specs_update) != len(replication_specs):
        print(f"✗ Error: replicationSpecs count mismatch!")
        return False
    
    # Process all replicationSpecs
    provider_settings = cluster_info.get("providerSettings", {})
    provider_name = provider_settings.get("providerName", "AWS")
    
    for spec in replication_specs_update:
        spec.pop("id", None)
        spec.pop("numShards", None)
        spec.pop("zoneName", None)
        
        if "regionsConfig" in spec and "regionConfigs" not in spec:
            regions_config_obj = spec.pop("regionsConfig")
            region_configs = []
            for region_name, region_data in regions_config_obj.items():
                region_config = {
                    "priority": region_data.get("priority", 7),
                    "regionName": region_name,
                    "providerName": provider_name,
                }
                for key in ["electableSpecs", "analyticsSpecs", "readOnlySpecs", "autoScaling"]:
                    if key in region_data:
                        region_config[key] = region_data[key]
                region_configs.append(region_config)
            spec["regionConfigs"] = region_configs
    
    # Update target shards
    updated_count = 0
    for shard_index in shards_to_scale:
        if shard_index < 0 or shard_index >= len(replication_specs_update):
            print(f"  ✗ Error: shard_index {shard_index} out of range")
            continue
        
        spec = replication_specs_update[shard_index]
        region_configs = spec.get("regionConfigs", [])
        if not region_configs:
            print(f"  ✗ Error: No region configs found for shard[{shard_index}]")
            continue
        
        region_config = region_configs[0]
        
        # Get current disk size to preserve it
        current_disk_size = 80.0
        if "electableSpecs" in region_config:
            current_disk_size = region_config["electableSpecs"].get("diskSizeGB", 80.0)
        
        # Update electableSpecs
        if "electableSpecs" in region_config:
            region_config["electableSpecs"]["instanceSize"] = scale_up_tier
            region_config["electableSpecs"]["nodeCount"] = DEFAULT_NODE_COUNT
            region_config["electableSpecs"]["diskSizeGB"] = int(current_disk_size)
            region_config["electableSpecs"].pop("diskIOPS", None)
            region_config["electableSpecs"]["ebsVolumeType"] = "STANDARD"
            print(f"  Updated shard[{shard_index}]: {base_tier} -> {scale_up_tier}, disk: {current_disk_size}GB")
            updated_count += 1
        else:
            print(f"  ✗ Error: No electableSpecs found for shard[{shard_index}]")
            continue
    
    if updated_count == 0:
        print(f"\n✗ No shards were updated for {cluster_name}")
        return False
    
    # Remove old format fields
    update_payload.pop("autoScaling", None)
    update_payload.pop("providerSettings", None)
    update_payload.pop("diskSizeGB", None)
    
    # Verify all replicationSpecs are included
    final_replication_specs = update_payload.get("replicationSpecs", [])
    if len(final_replication_specs) != original_count:
        print(f"✗ Error: replicationSpecs count mismatch! Original: {original_count}, Update: {len(final_replication_specs)}")
        return False
    
    print(f"\n  Making PATCH request with {len(final_replication_specs)} replicationSpecs (preserving all shards)...")
    
    # Make PATCH request
    url = f"https://cloud.mongodb.com/api/atlas/v2/groups/{project_id}/clusters/{cluster_name}"
    headers = {
        "Content-Type": "application/vnd.atlas.2024-10-23+json",
        "Accept": "application/vnd.atlas.2024-10-23+json"
    }
    auth = HTTPDigestAuth(public_key, private_key)
    
    try:
        response = requests.patch(url, json=update_payload, headers=headers, auth=auth)
        response.raise_for_status()
        print(f"✓ Scale-up request submitted successfully for {updated_count} shard(s)")
        return True, shards_to_scale
    except Exception as e:
        print(f"✗ Error updating cluster: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"  Response: {e.response.text}")
        return False, []


def main():
    parser = argparse.ArgumentParser(
        description="Scale up all clusters from baseTier to scaleUpTier",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument("--project-id", type=str, default=os.getenv("ATLAS_PROJECT_ID"),
                       help="MongoDB Atlas project ID")
    parser.add_argument("--public-key", type=str, default=os.getenv("ATLAS_PUBLIC_KEY"),
                       help="MongoDB Atlas public API key")
    parser.add_argument("--private-key", type=str, default=os.getenv("ATLAS_PRIVATE_KEY"),
                       help="MongoDB Atlas private API key")
    parser.add_argument("--config-file", type=str, default="clusterConfig.json",
                       help="JSON file with cluster information")
    
    args = parser.parse_args()
    
    if not all([args.project_id, args.public_key, args.private_key]):
        print("Error: --project-id, --public-key, and --private-key are required")
        sys.exit(1)
    
    # Read config
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, args.config_file)
    
    if not os.path.exists(config_path):
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)
    
    try:
        with open(config_path, 'r') as f:
            cluster_entries = json.load(f)
    except Exception as e:
        print(f"Error reading config file: {e}")
        sys.exit(1)
    
    # Process each cluster
    success_count = 0
    total_clusters = 0
    
    for entry in cluster_entries:
        cluster_name = (entry.get('clusterName') or '').strip()
        base_tier = (entry.get('baseTier') or '').strip()
        scale_up_tier = (entry.get('scaleUpTier') or '').strip()
        
        if not all([cluster_name, base_tier, scale_up_tier]):
            print(f"Skipping {cluster_name or 'unknown'}: Missing required fields")
            continue
        
        shards = entry.get('shards', [])
        if not shards:
            print(f"Skipping {cluster_name}: No shards configured")
            continue
        
        # Get shard indices
        shard_indices = [shard.get('shardIndex') for shard in shards if shard.get('shardIndex') is not None]
        if not shard_indices:
            print(f"Skipping {cluster_name}: No valid shard indices")
            continue
        
        total_clusters += 1
        success, updated_shards = scale_up_cluster(args.project_id, cluster_name, base_tier, scale_up_tier,
                                                  shard_indices, args.public_key, args.private_key)
        if success:
            success_count += 1
            # Update lastTierUpdate for successfully scaled shards
            if updated_shards:
                update_config_timestamp(config_path, cluster_name, updated_shards)
    
    # Summary
    print(f"\n{'='*60}")
    print(f"Summary: {success_count}/{total_clusters} cluster(s) processed successfully")
    print(f"{'='*60}")
    
    if success_count == total_clusters:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()

