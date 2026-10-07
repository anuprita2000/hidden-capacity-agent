# Hidden Capacity Agent

**Before you buy capacity, find the capacity you already have, one bottleneck at a time.**

A press line has 5 machines. Press 3 is the bottleneck, and the plant wants a new ~$1M press to hit its output target. Each morning this agent reads machine (PLC) and SAP data, works out how many hours the bottleneck really lost and why, and tells each owner what to fix. It also answers one question for the plant manager: **is the new press needed? Yes / No / Not yet.**

> Rules do all the maths. Claude only explains and drafts. People decide.

## Who decides what

| Owner | Decision | Cost if wrong |
|---|---|---|
| Plant manager | Approve or deny the new press | ~$1M capex on capacity they already had |
| Maintenance | Fix the short stops | Lost bottleneck hours = lost output |
| Industrial engineer | Correct the cycle time | Wrong capacity numbers lead to wrong decisions |
| Shift lead | Fix start-up, break and job-change coverage | Idle bottleneck hours every shift |

## Daily flow (6 am, after shifts close)

1. **Data health**: PLC counts vs SAP bookings, gaps in the PLC log, shifts with zero scrap booked. If the bottleneck's data fails, the agent reports that instead of OEE.
2. **True cycle time**: median time between parts from the PLC vs the SAP standard. Flags gaps above 10%.
3. **Press 3 metrics**: OEE = availability × performance × quality, plus AUR (run time ÷ calendar time) and good parts per hour.
4. **Loss finder**: sorts lost hours into short stops, breakdowns, idle after start-up/breaks, idle at job changes, slow running and scrap, then keeps the top 3.
5. **Capacity verdict**: compares current output, the SAP view of capacity and true capacity against demand. Recovering X hours gives Y parts, which answers whether the new press is needed.
6. **AI step (Claude API)**: drafts one alert per owner covering the loss, the likely cause, the floor action and the hours and dollars at stake. If no API key is set, a plain template writes the alerts instead.
7. **Log**: each day's hours lost and recovered are logged for the trend chart.

## Example output (from the included fake data)

```
1. Data health: WARN
   - [WARN] P3: 2026-10-02 shift B: zero scrap booked (512 good). Scrap was likely booked as good.
2. Cycle time: SAP 42.0 s vs PLC 36.0 s (16.7%)  << FLAG
3. OEE 72.5% = A 76.0% x P 97.3% x Q 98.1%
4. Lost 4.13 h. Top 3:
   - Short stops: 1.61 h, 51 events -> Maintenance
   - Idle at job changes: 1.3 h, 4 events -> Shift lead
   - Idle after start-up / breaks: 0.69 h, 4 events -> Shift lead
5. Capacity: output 1090 vs demand 1250 (SAP thinks max 1285, true max 1500). New press: Not yet
```

> **Maintenance:** Press 3 lost 1.61 h yesterday to short stops (51 events, 41 of them between 14:00 and 18:00), likely feeder jams or sensor faults. Recovering half = 80 parts/day = $338/day.
>
> **Plant manager:** Output 1090 vs demand 1250. New press: **Not yet**. Recovering 50% of the top 3 losses (1.8 h/day) adds 180 parts/day and closes the 160-part gap. Worth $226,800/yr against a $1,000,000 press.

## Data (fake CSVs that stand in for PLC and SAP)

| File | Stands in for | Columns |
|---|---|---|
| `plc_state_log.csv` | PLC / machine data collection | machine, timestamp, state (running/idle/stopped), part_count, order_id |
| `sap_machine_master.csv` | SAP standard cycle times | machine, description, std_cycle_time_s |
| `sap_bookings.csv` | SAP production confirmations | date, shift, machine, good_qty, scrap_qty |
| `shift_calendar.csv` | Plant calendar | date, shift, start, end, break_start, break_end, is_open |
| `costs.csv` | Finance | contribution margin per part, new press capex, production days, recovery target |
| `demand.csv` | Customer demand | date, machine, parts_required |

The agent has to find these planted problems on its own: an inflated SAP cycle time (42 s vs 36 s real), a cluster of short stops from 14:00 to 18:00, idle time after start-up and breaks, one shift with zero scrap booked, and a 40-minute hole in Press 5's PLC log.

## Run it

```bash
pip3 install -r requirements.txt
python3 generate_data.py          # build the fake plant week
python3 run.py --backfill         # analyse every day (fills the trend chart)
python3 run.py --now              # today's run: alerts + Slack
streamlit run dashboard.py        # one-page dashboard
```

Optional setup:
- `export ANTHROPIC_API_KEY=...` turns on the Claude step. Without it, template alerts are used.
- `export SLACK_WEBHOOK_URL=...` posts alerts to Slack. Alerts are always saved to `outputs/alerts_<date>.md`.
- `python3 run.py --date 2026-10-02` analyses a specific day.
- `python3 run.py --now --no-ai` skips the Claude step.

Schedule it daily at 6 am with cron (`crontab -e`):

```
0 6 * * * cd /path/to/hidden-capacity-agent && /usr/bin/python3 run.py --now >> outputs/cron.log 2>&1
```

## Files

| File | Job |
|---|---|
| `generate_data.py` | Builds the fake plant data with the planted problems |
| `rules.py` | All the maths: health, cycle time, OEE, losses, verdict |
| `writer.py` | Claude drafts the owner alerts, with a template fallback |
| `notify.py` | Slack webhook + markdown copy |
| `run.py` | Daily runner and CLI |
| `dashboard.py` | Streamlit dashboard |

## Stack

Python + pandas (rules) · Claude API (writing) · Slack webhook (alerts) · Streamlit (dashboard) · cron (schedule)

## Next steps (out of scope)

Predictive maintenance on the short-stop pattern · load balancing across plants · inventory and cost per unit · cloud deployment with a live PLC/SAP connection.
