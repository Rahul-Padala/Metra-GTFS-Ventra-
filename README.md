# 🚆 Metra GTFS Schedule Validator & Viewer

A Python-based tool for validating and viewing **Metra GTFS Schedule Data** using both **Static GTFS files** and **GTFS-Realtime feeds**.

This tool helps testers and QA engineers verify that newly deployed GTFS files are correctly reflected in the mobile application by comparing schedule data directly from GTFS feeds.

## Features

### Static GTFS Validation
- Load GTFS data from a ZIP file or extracted GTFS directory.
- View all available Metra lines.
- Browse stations in route order.
- Search journeys between departure and destination stations.
- View train schedules for any service date.

### Realtime Monitoring
- Live train locations (GPS).
- Trip updates and delay information.
- Service alerts.
- Cancellation detection.
- Added service detection.
- Realtime arrival predictions.

### Tester Benefits
- Validate newly deployed GTFS feeds.
- Verify train schedules displayed in the mobile application.
- Validate station ordering and route mapping.
- Cross-check realtime and static feed data.
- Quickly identify feed discrepancies after GTFS deployments.

## Prerequisites

```bash
Python 3.10+
```

### Required Packages

```bash
pip install pandas requests gtfs-realtime-bindings
```

## Project Structure

```text
project/
├── metra_schedule6.py
├── schedule.zip
└── README.md
```

## Running the Application

### Static Mode

```bash
python metra_schedule6.py --gtfs schedule.zip
```

### Realtime Mode

```bash
python metra_schedule6.py --gtfs schedule.zip --token YOUR_METRA_API_KEY
```

## Validation Use Cases

- Verify schedule accuracy after GTFS deployment.
- Verify station sequence.
- Verify train timings.
- Verify active services.
- Verify route mapping.
- Verify realtime delay information.
- Verify cancellations and alerts.

## Example QA Workflow

1. Download the latest GTFS package.
2. Load the GTFS package into the tool.
3. Select route and stations.
4. Compare tool output with mobile app data.
5. Validate schedules, routes, and realtime information.
6. Report discrepancies if any mismatch is found.

## Repository Description

Metra GTFS validation and schedule viewer tool for verifying newly deployed GTFS feeds, comparing mobile app data with official GTFS schedules, and monitoring realtime train operations.
