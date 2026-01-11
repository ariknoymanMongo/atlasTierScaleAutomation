# Atlas Cluster Auto-Scale-Down Monitor

## Overview

The `monitor_and_scale_down.py` script automatically monitors MongoDB Atlas sharded clusters and scales them back down to their `baseTier` when Atlas auto-scales them up to `scaleUpTier`. The script performs comprehensive safety checks before scaling down to ensure cluster stability.

## Purpose

Atlas can automatically scale up cluster tiers based on resource utilization. This script ensures that clusters are scaled back down to their base tier when:
1. Atlas has auto-scaled a shard up to `scaleUpTier`
2. Sufficient time has passed since the scale-up event
3. All safety validation checks pass

## Requirements

```bash
pip install requests python-dotenv
```

## Configuration Files

### `clusterConfig.json`
Defines clusters, their tiers, and per-shard tracking:

```json
[
  {
    "clusterName": "Cluster0",
    "baseTier": "M30",
    "scaleUpTier": "M40",
    "shards": [
      {
        "shardIndex": 0,
        "lastTierUpdate": "2026-01-08T10:00:00+00:00"
      },
      {
        "shardIndex": 1,
        "lastTierUpdate": "2026-01-08T10:00:00+00:00"
      }
    ]
  }
]
```

**Fields:**
- `clusterName`: Name of the Atlas cluster
- `baseTier`: Target tier for scale-down (e.g., "M30")
- `scaleUpTier`: Tier that Atlas scales up to (e.g., "M40")
- `shards`: Array of shard configurations
  - `shardIndex`: Index of the shard in `replicationSpecs` array
  - `lastTierUpdate`: ISO timestamp of last tier change (updated automatically)

### `tierConfig.csv`
Defines tier specifications for validation thresholds:

```csv
tier,cpu,ram,connection,iops
M10,2,2,1500,1000
M20,2,4,3000,2000
M30,2,8,3000,3000
M40,4,16,6000,3000
```

**Fields:**
- `tier`: Tier name (e.g., "M30")
- `cpu`: CPU value (for reference, not used in validation)
- `ram`: RAM capacity in GB for the tier
- `connection`: Maximum connection limit for the tier
- `iops`: Maximum IOPS limit for the tier

## Usage

```bash
python monitor_and_scale_down.py \
  --project-id PROJECT_ID \
  --public-key PUBLIC_KEY \
  --private-key PRIVATE_KEY \
  --min-hours-since-update 4 \
  --config-file clusterConfig.json \
  --tier-specs tierConfig.csv
```

**Arguments:**
- `--project-id`: MongoDB Atlas project ID (required)
- `--public-key`: Atlas public API key (required)
- `--private-key`: Atlas private API key (required)
- `--min-hours-since-update`: Minimum hours since last tier update before scaling down (default: 4)
- `--config-file`: Path to cluster configuration JSON file (default: `clusterConfig.json`)
- `--tier-specs`: Path to tier specifications CSV file (default: `tierConfig.csv`)

**Environment Variables:**
You can also set these via environment variables:
- `ATLAS_PROJECT_ID`
- `ATLAS_PUBLIC_KEY`
- `ATLAS_PRIVATE_KEY`
- `ATLAS_METRICS_PERIOD` (default: "PT30M")

## Flow and Logic

### High-Level Flow

```
For each cluster in clusterConfig.json:
  For each shard in cluster:
    1. Get current shard tier from Atlas
    2. Check if shard is at scaleUpTier
    3. If yes, check if it's a new scale-up event
    4. If new scale-up: Update timestamp, skip scale-down
    5. If not new: Check if enough time has passed
    6. If enough time: Run safety validations
    7. If all validations pass: Scale down to baseTier
```

### Detailed Flow

#### 1. Initial Checks

**Tier State Check:**
- If shard is at `baseTier`: No action needed, skip
- If shard is at `scaleUpTier`: Proceed with checks
- If shard is at any other tier: Skip (invalid state)

#### 2. Tier Specification Check

- Verifies that `baseTier` exists in `tierConfig.csv`
- If missing: Scale-down is blocked

#### 3. Autoscale Limits Check

- Validates that `baseTier` and `scaleUpTier` are within Atlas autoscale limits
- Checks `minInstanceSize` and `maxInstanceSize` from Atlas cluster configuration
- Both tiers must be within the configured autoscale range
- If outside limits: Scale-down is blocked

#### 4. New Scale-Up Detection

**Purpose:** Detect when Atlas first auto-scales a shard up.

**Logic:**
- If `lastTierUpdate` is missing or very old (>24 hours): Treat as new scale-up event
- Update `lastTierUpdate` to current time
- Skip scale-down (first detection, no action)

**Why:** Prevents immediate scale-down after Atlas auto-scales up. Gives Atlas time to handle the increased load.

#### 5. Time-Based Check

**Purpose:** Ensure sufficient time has passed since the scale-up event.

**Logic:**
- Check if `min_hours_since_update` (default: 4 hours) have passed since `lastTierUpdate`
- If not enough time: Skip scale-down, wait for next run
- If enough time: Proceed to safety validations

**Why:** Prevents premature scale-down. Ensures the cluster has been stable at the higher tier for a reasonable period.

#### 6. Safety Validations

**Purpose:** Ensure it's safe to scale down without impacting performance.

All validations use metrics from the last 30 minutes (configurable via `ATLAS_METRICS_PERIOD`).

##### 6.1 CPU Usage Check
- **Metric:** Average Normalized CPU Utilization
- **Threshold:** < 35% (constant threshold)
- **Validation:** `cpu_avg < 35.0`
- **Failure:** If CPU average >= 35%, scale-down is blocked

##### 6.2 Memory Usage Check
- **Metric:** Average Projected Total Memory Usage (GB)
- **Threshold:** < 60% of current tier's RAM capacity
- **Calculation:** `memory_avg_gb < (current_tier_ram * 0.6)`
- **Example:** For M40 (16GB RAM), threshold is 9.6GB
- **Failure:** If memory average >= 60% of current tier's RAM, scale-down is blocked

##### 6.3 IOPS Check
- **Metric:** Average Projected Total IOPS
- **Threshold:** < 60% of current tier's IOPS limit
- **Calculation:** `iops_avg < (current_tier_iops * 0.6)`
- **Example:** For M40 (3000 IOPS), threshold is 1800 IOPS
- **Failure:** If IOPS average >= 60% of current tier's IOPS, scale-down is blocked

##### 6.4 Connections Check
- **Metric:** Average number of connections
- **Threshold:** < 50% of baseTier's connection limit
- **Calculation:** `connections_avg < (base_tier_connection * 0.5)`
- **Example:** For baseTier M30 (3000 connections), threshold is 1500 connections
- **Failure:** If connections average >= 50% of baseTier's connection limit, scale-down is blocked

**Note:** All validations must pass. If any validation fails, scale-down is blocked and the reason is logged.

#### 7. Scale-Down Execution

**If all checks pass:**
1. Collect all shards that need scaling down
2. Make a single PATCH request to Atlas API
3. Update all target shards in one operation
4. Preserve all `replicationSpecs` (prevents shard loss)
5. Update `lastTierUpdate` timestamp for each scaled shard

**Note:** The script does NOT modify autoscale settings (`minInstanceSize`/`maxInstanceSize`). These remain as configured in Atlas.

**Important:** The script always includes ALL `replicationSpecs` in the PATCH request to prevent accidental shard removal.

## Validation Summary

| Check | Type | Threshold | Source |
|-------|------|-----------|--------|
| Tier Specification | Existence | Must exist in tierConfig.csv | Config file |
| Autoscale Limits | Range | Within min/max from Atlas | Atlas API |
| New Scale-Up Detection | Time | >24 hours old or missing | Config file |
| Time Since Update | Time | >= min_hours (default: 4) | Config file |
| CPU Usage | Metric | < 35% average | Atlas Metrics API |
| Memory Usage | Metric | < 60% of current tier RAM | Atlas Metrics API + tierConfig.csv |
| IOPS | Metric | < 60% of current tier IOPS | Atlas Metrics API + tierConfig.csv |
| Connections | Metric | < 50% of baseTier connection limit | Atlas Metrics API + tierConfig.csv |

## Example Scenarios

### Scenario 1: New Scale-Up Detected

```
1. Atlas auto-scales Cluster0 shard[1] from M30 → M40
2. Script runs and detects shard is at M40 (scaleUpTier)
3. Checks lastTierUpdate: 36 hours old (>24h threshold)
4. Treats as new scale-up event
5. Updates lastTierUpdate to now
6. Skips scale-down (first detection)
```

### Scenario 2: Scale-Down After Waiting Period

```
1. Shard is at M40 (scaleUpTier)
2. lastTierUpdate was set 4 hours ago
3. Time check passes (4 hours >= 3 hours required)
4. All safety checks pass:
   - CPU: 25% < 35% ✓
   - Memory: 8GB < 9.6GB (60% of M40's 16GB) ✓
   - IOPS: 1500 < 1800 (60% of M40's 3000) ✓
   - Connections: 1200 < 1500 (50% of M30's 3000) ✓
5. Scales down: M40 → M30
6. Updates lastTierUpdate
```

### Scenario 3: Safety Check Failure

```
1. Shard is at M40 (scaleUpTier)
2. Time check passes (enough time has passed)
3. Safety checks:
   - CPU: 25% < 35% ✓
   - Memory: 10GB >= 9.6GB ✗ (FAILED)
   - IOPS: 1500 < 1800 ✓
   - Connections: 1200 < 1500 ✓
4. Scale-down blocked due to high memory usage
5. Logs failure reason
6. Will check again in next run
```

## Key Features

1. **Per-Shard Tracking:** Each shard has its own `lastTierUpdate` timestamp
2. **Batch Updates:** Multiple shards within a cluster are updated in a single API call (1 API call per cluster)
3. **Shard Preservation:** Always includes all `replicationSpecs` to prevent shard loss
4. **Comprehensive Validation:** Multiple safety checks before scaling down
5. **Time-Based Logic:** Prevents immediate scale-down after Atlas auto-scales up
6. **Idempotent:** Safe to run multiple times (scheduling handled externally)

## Scheduling

The script is designed to run independently each time (not as a continuous loop). Schedule it using your task manager (cron, systemd, etc.):

**Example cron job (every 30 minutes):**
```bash
*/30 * * * * /usr/bin/python3 /path/to/monitor_and_scale_down.py --project-id PROJECT_ID --public-key KEY --private-key KEY
```

## Error Handling

- **API Errors:** Script logs errors and continues with next shard/cluster
- **Missing Config:** Script logs warnings and skips affected items
- **Validation Failures:** Script logs reasons and skips scale-down
- **Timestamp Parsing Errors:** Treated as new scale-up (safe default)

## Output

The script provides detailed logging:
- `✓` indicates successful checks or operations
- `✗` indicates failures or blocked operations
- `⏳` indicates waiting period (not enough time passed)
- `→` indicates informational messages

## Important Notes

1. **No Continuous Loop:** The script runs once per invocation. Schedule it externally.
2. **First Detection:** New scale-ups are detected and timestamped, but not scaled down immediately.
3. **Time Window:** Default 4-hour wait period prevents premature scale-down.
4. **All Validations Required:** All safety checks must pass for scale-down to proceed.
5. **Shard Preservation:** The script always preserves all shards in the cluster.

## Troubleshooting

**Issue:** Shards not scaling down
- Check if `lastTierUpdate` is recent (may be in waiting period)
- Verify all safety checks are passing (check logs)
- Ensure `baseTier` and `scaleUpTier` are in `tierConfig.csv`
- Verify autoscale limits allow the tier range

**Issue:** "Could not find primary process"
- Check that shard index matches `replicationSpecs` array index
- Verify cluster name matches Atlas cluster name
- Check API credentials have proper permissions

**Issue:** "No tier specs found"
- Verify `tierConfig.csv` contains the required tier
- Check CSV file format matches expected structure
- Ensure file path is correct

## API Endpoints Used

- `GET /api/atlas/v2/groups/{projectId}/clusters/{clusterName}` - Get cluster details
- `GET /api/atlas/v1.0/groups/{projectId}/processes` - Get all processes
- `GET /api/atlas/v1.0/groups/{projectId}/processes/{processId}/measurements` - Get metrics
- `PATCH /api/atlas/v2/groups/{projectId}/clusters/{clusterName}` - Update cluster tiers

## Metrics Collected

- **CPU_USER:** CPU utilization percentage
- **MEMORY_RESIDENT:** Memory usage in bytes (converted to GB)
- **DISK_PARTITION_IOPS_TOTAL:** Total IOPS
- **CONNECTIONS:** Number of active connections

All metrics are collected over the last 30 minutes (configurable) with 1-minute granularity.

