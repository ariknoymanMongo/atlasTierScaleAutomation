# Scale Up All Clusters Script

## Overview

The `scale_up_all.py` script is designed to scale up all shards in all clusters from their `baseTier` to `scaleUpTier`. This script performs no validation checks - it simply checks if shards are at the `baseTier` and scales them up to `scaleUpTier`.

**Use Case**: Scheduled scale-up operations (e.g., scaling up in the morning for peak business hours)

## Features

- **Simple and Fast**: No validation checks - just scale up if at `baseTier`
- **Multi-Cluster Support**: Processes all clusters defined in `clusterConfig.json`
- **Multi-Shard Support**: Scales all shards in each cluster simultaneously
- **Safe Updates**: Preserves all shards in the cluster (only updates target shards)
- **Batch Updates**: Uses a single PATCH request per cluster to update multiple shards
- **Config Tracking**: Automatically updates `lastTierUpdate` timestamp in `clusterConfig.json` after successful scale-up

## Requirements

- Python 3.6+
- Required packages:
  ```bash
  pip install requests python-dotenv
  ```

## Configuration

### clusterConfig.json

The script reads cluster configuration from `clusterConfig.json` in the same directory as the script. Each cluster entry must include:

- `clusterName`: Name of the cluster in Atlas
- `baseTier`: The lower tier (current state before scale-up)
- `scaleUpTier`: The target tier (where shards should be scaled to)
- `shards`: Array of shard configurations, each with:
  - `shardIndex`: Index of the shard in the `replicationSpecs` array
  - `lastTierUpdate`: ISO timestamp (automatically updated by the script after scale-up)

Example:
```json
[
  {
    "clusterName": "Cluster0",
    "baseTier": "M30",
    "scaleUpTier": "M40",
    "shards": [
      {
        "shardIndex": 0
      },
      {
        "shardIndex": 1
      },
      {
        "shardIndex": 2
      }
    ]
  },
  {
    "clusterName": "Cluster1",
    "baseTier": "M10",
    "scaleUpTier": "M20",
    "shards": [
      {
        "shardIndex": 0
      },
      {
        "shardIndex": 1
      }
    ]
  }
]
```

## Usage

### Command Line Arguments

```bash
python scale_up_all.py --project-id PROJECT_ID --public-key KEY --private-key KEY [OPTIONS]
```

**Required Arguments:**
- `--project-id`: MongoDB Atlas project ID
- `--public-key`: MongoDB Atlas public API key
- `--private-key`: MongoDB Atlas private API key

**Optional Arguments:**
- `--config-file`: Path to cluster configuration file (default: `clusterConfig.json`)

### Environment Variables

You can set credentials via environment variables instead of command-line arguments:

- `ATLAS_PROJECT_ID`
- `ATLAS_PUBLIC_KEY`
- `ATLAS_PRIVATE_KEY`

### Example Usage

```bash
# Using command-line arguments
python scale_up_all.py \
  --project-id YOUR_PROJECT_ID \
  --public-key "your-public-key" \
  --private-key "your-private-key"

# Using environment variables (in .env file)
python scale_up_all.py
```

## How It Works

### Execution Flow

1. **Load Configuration**: Reads `clusterConfig.json` to get cluster and shard definitions
2. **For Each Cluster**:
   - Fetches current cluster details from Atlas API
   - For each shard in the configuration:
     - Checks current tier
     - If at `baseTier`: Marks for scale-up to `scaleUpTier`
     - If already at `scaleUpTier`: Skips (no action needed)
     - If at any other tier: Skips with a warning
   - If any shards need scaling:
     - Prepares update payload (preserving all shards)
     - Makes a single PATCH request to update all target shards
     - On success: Updates `lastTierUpdate` timestamp in `clusterConfig.json` for all scaled shards
3. **Summary**: Displays success/failure summary

### Scale-Up Logic

The script uses the following logic for each shard:

1. **Check Current Tier**: Queries Atlas API for the shard's current `instanceSize`
2. **Decision Matrix**:
   - If `currentTier == baseTier` → Scale up to `scaleUpTier` ✓
   - If `currentTier == scaleUpTier` → Already scaled up, skip ⊙
   - If `currentTier` is any other value → Skip with warning ⚠

**Important**: The script does NOT perform any validation checks:
- No CPU/memory/IOPS checks
- No connection limit checks
- No time-based restrictions
- No autoscale limit validation

It simply scales up if the shard is at `baseTier`.

### Update Process

For each cluster, the script:

1. **Fetches Cluster Details**: Gets the full cluster configuration from Atlas
2. **Prepares Update Payload**:
   - Removes read-only fields (id, mongoURI, etc.)
   - Converts `regionsConfig` to `regionConfigs` format if needed
   - Preserves all `replicationSpecs` (ensures no shards are lost)
3. **Updates Target Shards**:
   - For each shard marked for scale-up:
     - Updates `electableSpecs.instanceSize` to `scaleUpTier`
     - Preserves current `diskSizeGB`
     - Sets `nodeCount` to 3
     - Sets `ebsVolumeType` to "STANDARD"
     - Removes `diskIOPS` (not used)
4. **Submits Update**: Makes a single PATCH request with all updates
5. **Updates Config File**: On successful scale-up, updates `lastTierUpdate` timestamp in `clusterConfig.json` for all scaled shards

**Safety**: The script always includes all `replicationSpecs` in the update payload, ensuring no shards are accidentally removed.

**Timestamp Tracking**: The script automatically updates the `lastTierUpdate` field in `clusterConfig.json` after successfully scaling up shards. This timestamp is used by the `monitor_and_scale_down.py` script to enforce waiting periods before scaling down.

## Output

The script provides detailed output for each cluster and shard:

```
============================================================
Scaling up cluster: Cluster0
  baseTier: M30, scaleUpTier: M40
  Shards to scale: [0, 1, 2]
============================================================
  ✓ Shard[0] is at M30 - will scale up to M40
  ✓ Shard[1] is at M30 - will scale up to M40
  ✓ Shard[2] is at M30 - will scale up to M40
  Updated shard[0]: M30 -> M40, disk: 80.0GB
  Updated shard[1]: M30 -> M40, disk: 80.0GB
  Updated shard[2]: M30 -> M40, disk: 80.0GB

  Making PATCH request with 3 replicationSpecs (preserving all shards)...
✓ Scale-up request submitted successfully for 3 shard(s)
  Updated lastTierUpdate for Cluster0 shard(s) [0, 1, 2]
```

**Status Symbols:**
- `✓`: Shard will be scaled up
- `⊙`: Shard already at target tier (skipped)
- `⚠`: Warning (shard at unexpected tier, skipped)
- `✗`: Error

## Exit Codes

- `0`: All clusters processed successfully
- `1`: One or more clusters failed to process, or required arguments missing

## Use Cases

### Scheduled Morning Scale-Up

Schedule this script to run in the morning (e.g., via cron, Kubernetes CronJob, or task scheduler):

```bash
# Example: Run every weekday at 8 AM
0 8 * * 1-5 cd /path/to/script && python scale_up_all.py --project-id ... --public-key ... --private-key ...
```

### Manual Scale-Up

Run manually when you need to scale up clusters:

```bash
python scale_up_all.py --project-id PROJECT_ID --public-key KEY --private-key KEY
```

## Comparison with Other Scripts

### vs. monitor_and_scale_down.py

| Feature | scale_up_all.py | monitor_and_scale_down.py |
|---------|----------------|---------------------------|
| **Purpose** | Scale up from `baseTier` to `scaleUpTier` | Scale down from `scaleUpTier` to `baseTier` |
| **Validation** | None | Full validation (CPU, memory, IOPS, connections, time windows) |
| **Use Case** | Scheduled morning scale-up | Auto-scaling detection and scale-back |
| **Safety Checks** | No | Yes (multiple safety checks) |
| **When to Use** | Proactive scale-up | Reactive scale-down |

### vs. check_and_scale_down.py

| Feature | scale_up_all.py | check_and_scale_down.py |
|---------|----------------|-------------------------|
| **Purpose** | Scale up | Scale down with checks |
| **Direction** | Up (baseTier → scaleUpTier) | Down (scaleUpTier → baseTier) |
| **Validation** | None | Full validation |

## API Endpoints Used

- **GET** `/api/atlas/v2/groups/{projectId}/clusters/{clusterName}` - Fetch cluster details
- **PATCH** `/api/atlas/v2/groups/{projectId}/clusters/{clusterName}` - Update cluster (scale up shards)

## Error Handling

The script handles common errors gracefully:

- **Missing Cluster**: Prints error and continues with next cluster
- **Invalid Shard Index**: Skips the shard with a warning
- **API Errors**: Prints error message and response details
- **Invalid Configuration**: Skips cluster with error message

All errors are logged but don't stop processing of other clusters.

## Notes

1. **No Validation**: This script performs no validation checks - it trusts that you want to scale up regardless of current metrics.

2. **Idempotent**: Safe to run multiple times - if shards are already at `scaleUpTier`, they will be skipped.

3. **Batch Updates**: All shards in a cluster are updated in a single API call for efficiency.

4. **Preserves Settings**: Disk size, node count, and other settings are preserved during scale-up.

5. **Autoscale Settings**: The script does NOT modify Atlas autoscale `minInstanceSize` and `maxInstanceSize` settings - these remain as configured in Atlas.

6. **Timestamp Tracking**: After successfully scaling up shards, the script automatically updates the `lastTierUpdate` timestamp in `clusterConfig.json`. This timestamp is used by `monitor_and_scale_down.py` to track when scale-ups occurred and enforce waiting periods before attempting scale-downs.

## Troubleshooting

### "Could not get cluster details"
- Verify cluster name matches exactly (case-sensitive)
- Check API credentials are correct
- Verify project ID is correct
- Check network connectivity to Atlas API

### "Shard is at {tier} (not baseTier or scaleUpTier) - skipping"
- The shard is at an unexpected tier
- May have been manually scaled or auto-scaled to a different tier
- Check cluster status in Atlas UI

### "No shards were updated"
- All shards are already at `scaleUpTier` (expected)
- Or all shards are at unexpected tiers (check cluster status)

### API Authentication Errors
- Verify `--public-key` and `--private-key` are correct
- Ensure API key has appropriate permissions (Project Owner or Cluster Manager role)
- Check if API key is enabled in Atlas

## Related Scripts

- `monitor_and_scale_down.py`: Monitor and scale down after Atlas auto-scales up (with validation)
- `check_and_scale_down.py`: Manual scale-down with safety checks
- `scale_up_all.py`: Simple scale-up script (this script)

