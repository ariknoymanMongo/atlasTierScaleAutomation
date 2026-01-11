#!/usr/bin/env python3
"""
Atlas Cluster Auto-Scale-Down Monitor

This script monitors Atlas clusters and automatically scales them back down to baseTier
when Atlas auto-scales them up to scaleUpTier. It runs safety checks and respects
a minimum time window since the last tier update.

Requirements:
    pip install requests python-dotenv

Usage:
    python monitor_and_scale_down.py --project-id PROJECT_ID --public-key KEY --private-key KEY
    --min-hours-since-update 4
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
import requests
from dotenv import load_dotenv

load_dotenv()

# Constants
DEFAULT_METRICS_PERIOD = os.getenv("ATLAS_METRICS_PERIOD", "PT30M")
DEFAULT_NODE_COUNT = 3
DEFAULT_MIN_HOURS_SINCE_UPDATE = 4  # hours

# Validation thresholds
CPU_THRESHOLD = 35.0  # %
MEMORY_THRESHOLD_PERCENT = 0.6  # 60%
IOPS_THRESHOLD_PERCENT = 0.6  # 60%
CONNECTIONS_THRESHOLD_PERCENT = 0.5  # 50%
NEW_SCALE_UP_THRESHOLD_HOURS = 24  # hours


class AtlasAPIClient:
    """Client for interacting with MongoDB Atlas API"""
    
    def __init__(self, public_key: str, private_key: str, org_id: str = ""):
        self.public_key = public_key
        self.private_key = private_key
        self.org_id = org_id
        self.session = requests.Session()
        self.session.auth = requests.auth.HTTPDigestAuth(public_key, private_key)
    
    def get_processes(self, project_id: str) -> List[Dict]:
        """Get all processes for a project"""
        try:
            url = f"https://cloud.mongodb.com/api/atlas/v1.0/groups/{project_id}/processes"
            response = self.session.get(url)
            response.raise_for_status()
            return response.json().get("results", [])
        except requests.exceptions.RequestException:
            return []
    
    def get_measurements(self, project_id: str, process_id: str, metric_name: str,
                        granularity: str = "PT1M", period: str = "PT30M") -> Optional[Dict]:
        """Get measurements for a process"""
        try:
            url = f"https://cloud.mongodb.com/api/atlas/v1.0/groups/{project_id}/processes/{process_id}/measurements"
            params = {"granularity": granularity, "period": period, "m": metric_name}
            response = self.session.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException:
            return None


class ScaleDownMonitor:
    """Monitor Atlas clusters and scale them down when Atlas auto-scales them up."""
    
    def __init__(self, public_key: str, private_key: str, project_id: str,
                 metrics_period: str = "PT30M"):
        self.project_id = project_id
        self.client = AtlasAPIClient(public_key, private_key, org_id="")
        self.metrics_period = metrics_period
        self._script_dir = os.path.dirname(os.path.abspath(__file__))
    
    def _get_file_path(self, filename: str) -> str:
        """Get absolute path for a file in the script directory"""
        return os.path.join(self._script_dir, filename)
    
    def _parse_timestamp(self, timestamp_str: str) -> Optional[datetime]:
        """Parse ISO timestamp string to datetime object"""
        if not timestamp_str:
            return None
        try:
            if timestamp_str.endswith('Z'):
                dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            else:
                dt = datetime.fromisoformat(timestamp_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None
    
    def _get_region_config(self, cluster_info: Dict, shard_index: int) -> Optional[Dict]:
        """Extract region config for a specific shard"""
        replication_specs = cluster_info.get("replicationSpecs", [])
        if shard_index < 0 or shard_index >= len(replication_specs):
            return None
        
        spec = replication_specs[shard_index]
        region_configs = spec.get("regionConfigs", [])
        if not region_configs and spec.get("regionsConfig"):
            regions_obj = spec.get("regionsConfig", {})
            if regions_obj:
                region_configs = [list(regions_obj.values())[0]]
        
        return region_configs[0] if region_configs else None
    
    def _tier_to_number(self, tier_str: str) -> int:
        """Convert tier string (e.g., 'M30') to number (30)"""
        if not tier_str or not isinstance(tier_str, str):
            return 0
        try:
            if tier_str.upper().startswith(("M", "R")):
                return int(tier_str[1:])
            return int(tier_str)
        except (ValueError, TypeError):
            return 0
    
    def _extract_metric_values(self, measurement_data: Optional[Dict], 
                               transform=None) -> List[float]:
        """Extract metric values from measurement data"""
        if not measurement_data or "measurements" not in measurement_data:
            return []
        values = [m.get("value", 0) for m in measurement_data["measurements"] 
                 if m.get("value") is not None]
        if transform:
            values = [transform(v) for v in values]
        return values
    
    def load_tier_specs(self, tier_specs_file: str = "tierConfig.csv") -> Dict:
        """Load tier specifications from CSV file"""
        tier_specs = {}
        tier_specs_path = self._get_file_path(tier_specs_file)
        
        if not os.path.exists(tier_specs_path):
            print(f"Warning: Tier specs file not found: {tier_specs_path}")
            return tier_specs
        
        try:
            with open(tier_specs_path, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    tier = row.get('tier', '').strip()
                    if tier:
                        tier_specs[tier] = {
                            'ram': float(row.get('ram', 0)),
                            'connection': int(row.get('connection', 0)),
                            'iops': int(row.get('iops', 0))
                        }
        except Exception as e:
            print(f"Error loading tier specs: {e}")
        
        return tier_specs
    
    def get_cluster_details(self, cluster_name: str) -> Optional[Dict]:
        """Get cluster details using v2 API"""
        try:
            url = f"https://cloud.mongodb.com/api/atlas/v2/groups/{self.project_id}/clusters/{cluster_name}"
            headers = {"Accept": "application/vnd.atlas.2024-10-23+json"}
            response = self.client.session.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"Error getting cluster details: {e}")
            return None
    
    def get_cluster_process_for_shard(self, cluster_name: str, cluster_info: Dict,
                                     shard_index: int) -> Optional[Dict]:
        """Get primary process for a specific shard using index-based mapping"""
        replication_specs = cluster_info.get("replicationSpecs", [])
        if not replication_specs or shard_index < 0 or shard_index >= len(replication_specs):
            return None
        
        processes = self.client.get_processes(self.project_id)
        if not processes:
            return None
        
        cluster_id = cluster_name.lower().replace("cluster", "")
        candidates = []
        
        for p in processes:
            hostname = p.get("hostname", "")
            if cluster_id not in hostname.lower():
                continue
            
            replica_set = (p.get("replicaSetName") or "").lower()
            type_name = p.get("typeName", "")
            
            # Match based on shard_index
            matched = False
            if shard_index == 0:
                matched = "config-0" in replica_set or "config" in replica_set or "shard-0" in replica_set
            else:
                matched = f"shard-{shard_index - 1}" in replica_set
            
            if matched:
                if type_name in ["REPLICA_PRIMARY", "SHARD_CONFIG_PRIMARY"]:
                    return p
                candidates.append(p)
        
        return candidates[0] if candidates else None
    
    def get_cluster_metrics(self, cluster_name: str, cluster_info: Dict,
                           primary_process: Dict) -> Dict:
        """Get metrics for a cluster's primary process"""
        process_id = primary_process.get("id")
        if not process_id:
            return self._default_metrics()
        
        metrics = self._default_metrics()
        
        # CPU metrics
        cpu_data = self.client.get_measurements(
            self.project_id, process_id, "CPU_USER",
            granularity="PT1M", period=self.metrics_period
        )
        cpu_values = self._extract_metric_values(cpu_data)
        if cpu_values:
            metrics['cpu_max'] = max(cpu_values)
            metrics['cpu_avg'] = sum(cpu_values) / len(cpu_values)
            if len(cpu_values) > 1:
                variance = sum((x - metrics['cpu_avg']) ** 2 for x in cpu_values) / len(cpu_values)
                metrics['cpu_std'] = variance ** 0.5
        
        # Memory metrics
        memory_data = self.client.get_measurements(
            self.project_id, process_id, "MEMORY_RESIDENT",
            granularity="PT1M", period=self.metrics_period
        )
        memory_values = self._extract_metric_values(memory_data, lambda v: v / (1024**3))
        if memory_values:
            metrics['memory_max_gb'] = max(memory_values)
            metrics['memory_avg_gb'] = sum(memory_values) / len(memory_values)
        
        # IOPS metrics
        iops_data = self.client.get_measurements(
            self.project_id, process_id, "DISK_PARTITION_IOPS_TOTAL",
            granularity="PT1M", period=self.metrics_period
        )
        iops_values = self._extract_metric_values(iops_data)
        if iops_values:
            metrics['iops_max'] = max(iops_values)
            metrics['iops_avg'] = sum(iops_values) / len(iops_values)
        
        # Connections metrics
        conn_data = self.client.get_measurements(
            self.project_id, process_id, "CONNECTIONS",
            granularity="PT1M", period=self.metrics_period
        )
        conn_values = self._extract_metric_values(conn_data)
        if conn_values:
            metrics['connections_max'] = max(conn_values)
            metrics['connections_avg'] = sum(conn_values) / len(conn_values)
        
        return metrics
    
    def _default_metrics(self) -> Dict:
        """Return default metrics when measurement fetch fails"""
        return {
            'cpu_max': 0.0, 'cpu_avg': 0.0, 'cpu_std': 0.0,
            'memory_max_gb': 0.0, 'memory_avg_gb': 0.0,
            'iops_max': 0.0, 'iops_avg': 0.0,
            'connections_max': 0, 'connections_avg': 0.0
        }
    
    def check_safety_conditions(self, base_tier: str, current_tier: str,
                                metrics: Dict, tier_specs: Dict) -> Tuple[bool, List[str]]:
        """Check if it's safe to scale down to baseTier"""
        reasons = []
        
        if base_tier not in tier_specs or current_tier not in tier_specs:
            reasons.append(f"Missing tier specs for baseTier {base_tier} or current tier {current_tier}")
            return False, reasons
        
        current_spec = tier_specs[current_tier]
        base_spec = tier_specs[base_tier]
        
        # CPU check
        if metrics.get('cpu_avg', 0) >= CPU_THRESHOLD:
            reasons.append(f"CPU avg ({metrics['cpu_avg']:.2f}%) >= {CPU_THRESHOLD}% threshold")
        
        # Memory check
        current_ram = current_spec.get('ram', 0)
        if current_ram > 0:
            memory_threshold = current_ram * MEMORY_THRESHOLD_PERCENT
            if metrics.get('memory_avg_gb', 0) >= memory_threshold:
                reasons.append(f"Memory avg ({metrics['memory_avg_gb']:.2f}GB) >= 60% of {current_tier} RAM ({memory_threshold:.2f}GB)")
        else:
            reasons.append(f"Could not determine memory threshold for {current_tier}")
        
        # IOPS check
        current_iops = current_spec.get('iops', 0)
        iops_threshold = current_iops * IOPS_THRESHOLD_PERCENT
        if metrics.get('iops_avg', 0) >= iops_threshold:
            reasons.append(f"IOPS avg ({metrics['iops_avg']:.2f}) >= 60% of {current_tier} IOPS ({iops_threshold:.2f})")
        
        # Connections check
        base_connection = base_spec.get('connection', 0)
        connections_threshold = base_connection * CONNECTIONS_THRESHOLD_PERCENT
        if metrics.get('connections_avg', 0) >= connections_threshold:
            reasons.append(f"Connections avg ({metrics['connections_avg']:.2f}) >= 50% of {base_tier} connection limit ({connections_threshold:.2f})")
        
        return len(reasons) == 0, reasons
    
    def check_shard_tier(self, cluster_info: Dict, shard_index: int) -> Optional[str]:
        """Get current tier for a specific shard"""
        region_config = self._get_region_config(cluster_info, shard_index)
        if region_config:
            effective_specs = region_config.get("effectiveElectableSpecs", {})
            return effective_specs.get("instanceSize")
        return None
    
    def check_autoscale_limits(self, cluster_info: Dict, shard_index: int,
                               base_tier: str, scale_up_tier: str) -> Tuple[bool, List[str]]:
        """Check if baseTier and scaleUpTier are within autoscale limits"""
        reasons = []
        region_config = self._get_region_config(cluster_info, shard_index)
        if not region_config:
            reasons.append(f"No region config found for shard[{shard_index}]")
            return False, reasons
        
        compute_cfg = region_config.get("autoScaling", {}).get("compute", {})
        if not compute_cfg:
            reasons.append(f"No autoscale compute config for shard[{shard_index}]")
            return False, reasons
        
        min_instance_size = compute_cfg.get("minInstanceSize", "")
        max_instance_size = compute_cfg.get("maxInstanceSize", "")
        if not min_instance_size or not max_instance_size:
            reasons.append(f"Autoscale min/max not configured for shard[{shard_index}]")
            return False, reasons
        
        base_num = self._tier_to_number(base_tier)
        scale_up_num = self._tier_to_number(scale_up_tier)
        min_num = self._tier_to_number(min_instance_size)
        max_num = self._tier_to_number(max_instance_size)
        
        if base_num < min_num or base_num > max_num:
            reasons.append(f"baseTier {base_tier} outside autoscale limits [{min_instance_size}, {max_instance_size}]")
        if scale_up_num < min_num or scale_up_num > max_num:
            reasons.append(f"scaleUpTier {scale_up_tier} outside autoscale limits [{min_instance_size}, {max_instance_size}]")
        
        return len(reasons) == 0, reasons
    
    def check_time_since_update(self, last_tier_update: str, min_hours: int) -> Tuple[bool, Optional[timedelta]]:
        """Check if enough time has passed since last tier update"""
        if not last_tier_update:
            return True, None
        
        last_update = self._parse_timestamp(last_tier_update)
        if not last_update:
            return True, None
        
        time_diff = datetime.now(timezone.utc) - last_update
        hours_passed = time_diff.total_seconds() / 3600
        return hours_passed >= min_hours, time_diff
    
    def is_timestamp_very_old(self, last_tier_update: str, 
                             threshold_hours: int = NEW_SCALE_UP_THRESHOLD_HOURS) -> Tuple[bool, Optional[timedelta]]:
        """Check if timestamp is very old (likely stale, indicating a new scale-up event)"""
        if not last_tier_update:
            return True, None
        
        last_update = self._parse_timestamp(last_tier_update)
        if not last_update:
            return True, None
        
        time_diff = datetime.now(timezone.utc) - last_update
        hours_passed = time_diff.total_seconds() / 3600
        return hours_passed >= threshold_hours, time_diff
    
    def update_cluster_shards(self, cluster_name: str, shard_updates: List[Dict],
                              target_tier: str, scale_up_tier: str) -> bool:
        """Update multiple shards in a single PATCH request"""
        try:
            cluster_info = self.get_cluster_details(cluster_name)
            if not cluster_info:
                return False
            
            import copy
            update_payload = copy.deepcopy(cluster_info)
            original_replication_specs = cluster_info.get("replicationSpecs", [])
            
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
            
            replication_specs = update_payload.get("replicationSpecs", [])
            if len(replication_specs) != len(original_replication_specs):
                print(f"  ✗ ERROR: replicationSpecs count mismatch!")
                return False
            
            provider_name = cluster_info.get("providerSettings", {}).get("providerName", "AWS")
            update_payload.pop("autoScaling", None)
            update_payload.pop("providerSettings", None)
            update_payload.pop("diskSizeGB", None)
            
            # Process all replicationSpecs
            for spec in replication_specs:
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
            for update in shard_updates:
                shard_index = update['shard_index']
                current_disk_size = update['current_disk_size']
                
                if shard_index < 0 or shard_index >= len(replication_specs):
                    print(f"  ✗ Error: shard_index {shard_index} out of range")
                    continue
                
                region_config = self._get_region_config(update_payload, shard_index)
                if not region_config:
                    print(f"  ✗ Error: No region configs found for shard[{shard_index}]")
                    continue
                
                # Update electableSpecs
                if "electableSpecs" in region_config:
                    region_config["electableSpecs"]["instanceSize"] = target_tier
                    region_config["electableSpecs"]["nodeCount"] = DEFAULT_NODE_COUNT
                    region_config["electableSpecs"]["diskSizeGB"] = int(current_disk_size)
                    region_config["electableSpecs"].pop("diskIOPS", None)
                    region_config["electableSpecs"]["ebsVolumeType"] = "STANDARD"
                    print(f"  Updated replicationSpecs[{shard_index}] to {target_tier}, disk: {current_disk_size}GB")
                else:
                    print(f"  ✗ Error: No electableSpecs found for shard[{shard_index}]")
                    continue
                
                # Note: We do NOT modify autoscale settings - they remain as configured in Atlas
            
            # Verify all replicationSpecs are included
            final_replication_specs = update_payload.get("replicationSpecs", [])
            if len(final_replication_specs) != len(original_replication_specs):
                print(f"  ✗ FATAL ERROR: Update payload is missing replicationSpecs!")
                return False
            
            # Make PATCH request
            url = f"https://cloud.mongodb.com/api/atlas/v2/groups/{self.project_id}/clusters/{cluster_name}"
            headers = {
                "Content-Type": "application/vnd.atlas.2024-10-23+json",
                "Accept": "application/vnd.atlas.2024-10-23+json"
            }
            
            print(f"  Making PATCH request with {len(final_replication_specs)} replicationSpecs...")
            response = self.client.session.patch(url, json=update_payload, headers=headers)
            response.raise_for_status()
            
            print(f"  ✓ Scale-down request submitted for {len(shard_updates)} shard(s) to {target_tier}")
            return True
        except Exception as e:
            print(f"  ✗ Error updating cluster: {e}")
            return False
    
    def update_config_timestamp(self, config_file: str, cluster_name: str, shard_index: int):
        """Update lastTierUpdate timestamp for a shard"""
        try:
            config_path = self._get_file_path(config_file)
            with open(config_path, 'r') as f:
                data = json.load(f)
            
            now_iso = datetime.now(timezone.utc).isoformat()
            for entry in data:
                if entry.get('clusterName') == cluster_name:
                    for shard in entry.get('shards', []):
                        if shard.get('shardIndex') == shard_index:
                            shard['lastTierUpdate'] = now_iso
                            break
            
            with open(config_path, 'w') as f:
                json.dump(data, f, indent=2)
            
            print(f"  Updated lastTierUpdate for {cluster_name} shard[{shard_index}]")
        except Exception as e:
            print(f"  Warning: Could not update config file: {e}")
    
    def check_and_scale_down_shard(self, cluster_name: str, shard_index: int,
                                   base_tier: str, scale_up_tier: str,
                                   last_tier_update: str, min_hours: int,
                                   config_file: str, tier_specs: Dict) -> Optional[Dict]:
        """Check a shard and scale down if needed"""
        print(f"\n  Checking shard[{shard_index}]...")
        
        cluster_info = self.get_cluster_details(cluster_name)
        if not cluster_info:
            print(f"  ✗ Could not get cluster details")
            return None
        
        current_tier = self.check_shard_tier(cluster_info, shard_index)
        if not current_tier:
            print(f"  ✗ Could not determine current tier")
            return None
        
        print(f"    Current tier: {current_tier}")
        
        # Tier state check
        if current_tier == base_tier:
            print(f"    Shard is at baseTier {base_tier} - no action needed")
            return None
        
        if current_tier != scale_up_tier:
            print(f"    Shard is at {current_tier} (not baseTier or scaleUpTier) - skipping")
            return None
        
        print(f"    Shard is at scaleUpTier {scale_up_tier} - checking if we should scale down...")
        
        # Tier specification check
        if base_tier not in tier_specs:
            print(f"    ✗ No tier specs found for baseTier {base_tier} - scale-down blocked")
            return None
        print(f"    ✓ Tier specification check passed")
        
        # Autoscale limits check
        autoscale_valid, autoscale_reasons = self.check_autoscale_limits(
            cluster_info, shard_index, base_tier, scale_up_tier
        )
        if not autoscale_valid:
            print(f"    ✗ Autoscale limits check failed:")
            for reason in autoscale_reasons:
                print(f"      - {reason}")
            return None
        print(f"    ✓ Autoscale limits check passed")
        
        # Check for new scale-up event
        is_very_old, time_diff_old = self.is_timestamp_very_old(last_tier_update)
        if is_very_old:
            hours_old = time_diff_old.total_seconds() / 3600 if time_diff_old else None
            status = f"(timestamp is {hours_old:.1f} hours old)" if hours_old else "(no previous timestamp)"
            print(f"    → Detected new scale-up event {status}")
            print(f"    → Updating lastTierUpdate to now (first detection, no scale-down)")
            self.update_config_timestamp(config_file, cluster_name, shard_index)
            return None
        
        # Time check
        can_scale, time_diff = self.check_time_since_update(last_tier_update, min_hours)
        if not can_scale:
            hours_passed = time_diff.total_seconds() / 3600 if time_diff else 0
            print(f"    ⏳ Waiting period: {hours_passed:.1f} hours < {min_hours} hours required")
            print(f"    → Will check again in next run")
            return None
        
        if time_diff:
            hours_passed = time_diff.total_seconds() / 3600
            print(f"    ✓ Time check passed: {hours_passed:.1f} hours >= {min_hours} hours")
        
        # Get metrics
        primary_process = self.get_cluster_process_for_shard(cluster_name, cluster_info, shard_index)
        if not primary_process:
            print(f"    ✗ Could not find primary process")
            return None
        
        print(f"    Fetching metrics...")
        metrics = self.get_cluster_metrics(cluster_name, cluster_info, primary_process)
        
        # Safety checks
        print(f"    Checking safety conditions...")
        safe, reasons = self.check_safety_conditions(base_tier, current_tier, metrics, tier_specs)
        if not safe:
            print(f"    ✗ Safety checks failed:")
            for reason in reasons:
                print(f"      - {reason}")
            return None
        
        print(f"    ✓ Safety checks passed")
        
        # Get current disk size
        region_config = self._get_region_config(cluster_info, shard_index)
        current_disk_size = 80.0
        if region_config:
            effective_specs = region_config.get("effectiveElectableSpecs", {})
            current_disk_size = effective_specs.get("diskSizeGB", 80.0)
        
        return {'shard_index': shard_index, 'current_disk_size': current_disk_size}
    
    def monitor_cluster(self, cluster_name: str, base_tier: str, scale_up_tier: str,
                       shards_config: List[Dict], min_hours: int,
                       config_file: str, tier_specs: Dict):
        """Monitor a single cluster and scale down shards if needed"""
        print(f"\n{'='*60}")
        print(f"Monitoring cluster: {cluster_name}")
        print(f"  baseTier: {base_tier}, scaleUpTier: {scale_up_tier}")
        print(f"{'='*60}")
        
        shard_updates = []
        for shard_config in shards_config:
            shard_index = shard_config.get('shardIndex')
            if shard_index is None:
                continue
            
            result = self.check_and_scale_down_shard(
                cluster_name, shard_index, base_tier, scale_up_tier,
                shard_config.get('lastTierUpdate', ''), min_hours, config_file, tier_specs
            )
            
            if isinstance(result, dict):
                shard_updates.append(result)
        
        if shard_updates:
            print(f"\n  Updating {len(shard_updates)} shard(s) in a single request...")
            if self.update_cluster_shards(cluster_name, shard_updates, base_tier, scale_up_tier):
                for update in shard_updates:
                    self.update_config_timestamp(config_file, cluster_name, update['shard_index'])
                print(f"\n✓ Scaled down {len(shard_updates)} shard(s) for {cluster_name}")
            else:
                print(f"\n✗ Failed to scale down shards for {cluster_name}")
        else:
            print(f"\n✓ No shards needed scaling down for {cluster_name}")


def main():
    parser = argparse.ArgumentParser(
        description="Monitor Atlas clusters and scale down when auto-scaled up",
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
    parser.add_argument("--tier-specs", type=str, default="tierConfig.csv",
                       help="CSV file with tier specifications")
    parser.add_argument("--min-hours-since-update", type=int, default=DEFAULT_MIN_HOURS_SINCE_UPDATE,
                       help=f"Minimum hours since lastTierUpdate before scaling down (default: {DEFAULT_MIN_HOURS_SINCE_UPDATE})")
    
    args = parser.parse_args()
    
    if not all([args.project_id, args.public_key, args.private_key]):
        print("Error: --project-id, --public-key, and --private-key are required")
        sys.exit(1)
    
    monitor = ScaleDownMonitor(args.public_key, args.private_key, args.project_id)
    tier_specs = monitor.load_tier_specs(args.tier_specs)
    config_path = monitor._get_file_path(args.config_file)
    
    if not os.path.exists(config_path):
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)
    
    with open(config_path, 'r') as f:
        cluster_entries = json.load(f)
    
    for entry in cluster_entries:
        cluster_name = (entry.get('clusterName') or '').strip()
        base_tier = (entry.get('baseTier') or '').strip()
        scale_up_tier = (entry.get('scaleUpTier') or '').strip()
        
        if not all([cluster_name, base_tier, scale_up_tier]):
            print(f"Skipping {cluster_name or 'unknown'}: Missing required fields")
            continue
        
        shards = entry.get('shards', [])
        if not shards:
            continue
        
        monitor.monitor_cluster(
            cluster_name, base_tier, scale_up_tier,
            shards, args.min_hours_since_update,
            args.config_file, tier_specs
        )


if __name__ == "__main__":
    main()
