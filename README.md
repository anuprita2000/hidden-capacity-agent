# Hidden Capacity Agent

**Before you buy capacity, find the capacity you already have, one bottleneck at a time.**

A plant has 5 presses, 2 weld cells and a packing station. It wants a new ~$1M press to hit its output target. Each morning this agent reads machine (PLC) and SAP data, finds the bottleneck, works out how many hours it really lost and why, and tells each owner what to fix. It also answers one question for the plant manager: **is new capacity needed? Yes / No / Not yet, and where?**

> Rules do all the maths. Claude only explains and drafts. People decide.

## Plant dashboard

`python3 -m streamlit run dashboard.py` opens a four-page dashboard:

| Page | For | Shows |
|---|---|---|
| **Plant overview** | Plant manager / leadership | Lines meeting demand, plant output vs demand, shortfall, recoverable $/yr, justified investment calls, data health; a card per line (status, flow, bottleneck, next bottleneck, value at stake); output vs demand by line; 6-day trend; **priority actions ranked by $**; status of every machine |
| **Lines** | Line / production manager | The flow with the bottleneck marked, line capacity, gap, headroom, a bullet chart of capacity per machine against demand, a deep-dive on the bottleneck, trends, and owner alerts |
| **Machines** | Maintenance / IE | Any machine on its own: OEE split, SAP vs PLC cycle time, losses, capacity vs demand (adjustable), OEE trend, alerts |
| **Line setup** | Admin | Create, rename and delete lines as **stations** in flow order; set demand. A station is one machine, several machines making **different parts for one assembly** (with parts per set), or several making the **same part side by side**. It blocks a machine from being used twice. **Save** makes the 6 am run use these lines |

## Stations: how machines feed each other

The same machines can give opposite answers depending on how they connect, so the line is described as stations:

| Station type | Example | Station capacity |
|---|---|---|
| Single machine | Weld cell 1 | that machine's good output |
| **Different parts → one assembly** | Press 1 (outer panel) + Press 3 (inner panel) → weld cell | complete sets = the smallest of (output ÷ parts per set). The slowest feeder limits it. |
| **Same part → outputs add** | two presses stamping the same bracket | the sum of their outputs |

On the sample data, Line A is **(Press 1 + Press 3) → Weld cell 1 → Packing 1**. Press 1 makes 1,574 outer panels but Press 3 only 1,098 inner panels, so the weld cell gets 1,098 complete sets. Press 3 is the bottleneck. If the two presses made the *same* part instead, they would supply 2,672/day, and Weld cell 1 would become the bottleneck.

## Two modes

| Mode | Use it when | What it finds |
|---|---|---|
| **Line** | 2–6 machines in one continuous flow, e.g. Press 1 → Press 3 → Weld 1 → Pack 1 | The bottleneck (the machine making the fewest good parts/day), line capacity, the next bottleneck and the headroom before it takes over. Losses and alerts focus on the bottleneck only, because an hour recovered anywhere else adds no output. |
| **Single machine** | One machine running on its own | That machine's OEE, losses, true capacity and verdict against its own demand |

Lines are saved on the Line setup page (config/lines.json); the 6 am run analyses every saved line.

**Line mode can change the decision.** On the sample data:
- **(Press 1 + Press 3) → Weld 1 → Pack 1:** Press 3 is the bottleneck at 1,098 sets/day against demand of 1,250. The verdict is **Not yet**: fix Press 3's short stops, job changes and start-up idle first.
- **Swap Weld 1 for Weld 2:** Press 3 is still today's bottleneck, but Weld 2 sits only 7 units above it and can't reach demand even with its losses recovered. The verdict is **Yes, add capacity at Weld cell 2**, not a new press.

**OEE range on the sample data:** every machine runs below 85% OEE on every day (79–83% for most machines, 73–76% for Press 3). `generate_data.py` re-checks this with the agent's own OEE maths and fails if any machine reaches 85%.

## Who decides what

| Owner | Decision | Cost if wrong |
|---|---|---|
| Plant manager | Approve or deny new capacity, and where | ~$1M capex on capacity they already had, or on the wrong machine |
| Maintenance | Fix short stops and breakdowns | Lost bottleneck hours = lost output |
| Industrial engineer | Correct cycle times | Wrong capacity numbers lead to wrong decisions |
| Shift lead | Fix start-up, break and job-change coverage | Idle bottleneck hours every shift |

## Daily flow (6 am, after shifts close)

1. **Data health** for every machine: PLC counts vs SAP bookings, gaps in the PLC log, shifts with zero scrap booked. If a machine in the setup fails, the agent reports that instead of OEE.
2. **True cycle time**: median time per cycle from the PLC vs the SAP standard. Flags gaps above 10%.
3. **Metrics** per machine: OEE = availability × performance × quality, plus AUR and good parts per hour. Parts per cycle is handled, for example a double-hit die.
4. **Loss finder**: sorts lost hours into short stops, breakdowns, idle after start-up/breaks, idle at job changes, other idle, slow running and scrap, then keeps the top 3.
5. **Bottleneck and verdict**: compares current output, output after recovering 50% of the top 3 losses, and true capacity against demand, for each machine in the flow.
6. **AI step (Claude API)**: drafts one alert per owner. If no API key is set, a template writes the alerts.
7. **Log**: the day's results are saved for the trend charts.

## Data (fake CSVs that stand in for PLC and SAP)

Every file uses the same machine IDs: `PRS-01`…`PRS-05`, `WLD-01`, `WLD-02`, `PCK-01`.

| File | Stands in for | Columns |
|---|---|---|
| `plc_state_log.csv` | PLC / machine data collection | machine, timestamp, state (running/idle/stopped), part_count, order_id |
| `sap_machine_master.csv` | SAP machine master | machine, description, type, std_cycle_time_s, parts_per_cycle |
| `sap_bookings.csv` | SAP production confirmations | date, shift, machine, good_qty, scrap_qty |
| `shift_calendar.csv` | Plant calendar | date, shift, start, end, break_start, break_end, is_open |
| `costs.csv` | Finance | margin per part, capex per machine type, production days, recovery target |
| `demand.csv` | Customer demand | date, target (machine ID or LINE), parts_required |

The agent has to find these planted problems on its own:
- PRS-03's SAP cycle time is 42 s; the press really runs at 36 s.
- PRS-03 has a cluster of short stops between 14:00 and 18:00.
- PRS-03 sits idle after start-up and breaks.
- One PRS-03 shift has zero scrap booked.
- PRS-05 has a 40-minute hole in its PLC log.
- WLD-02 loses a lot of time at job changes, which makes it the next bottleneck when it's in the line.

## Run it

```bash
pip3 install -r requirements.txt
python3 generate_data.py                 # build the fake plant week
python3 run.py --backfill                # fill the trend log
python3 run.py --now                     # every saved line: alerts + Slack
python3 -m streamlit run dashboard.py    # plant dashboard (4 pages)
```

Command-line overrides:
```bash
python3 run.py --now --line PRS-01+PRS-03,WLD-02,PCK-01   # + = assembled together at one station
python3 run.py --now --mode single --machine WLD-02
python3 run.py --date 2026-10-02 --no-ai
```

Optional setup:
- `export ANTHROPIC_API_KEY=...` turns on the Claude step. Without it, template alerts are used.
- `export SLACK_WEBHOOK_URL=...` posts alerts to Slack. Alerts are always saved to `outputs/`.

Schedule it daily at 6 am with cron (`crontab -e`):
```
0 6 * * * cd /path/to/hidden-capacity-agent && /usr/bin/python3 run.py --now >> outputs/cron.log 2>&1
```

## Files

| File | Job |
|---|---|
| `generate_data.py` | Builds the fake plant data with the planted problems |
| `rules.py` | All the maths: health, cycle time, OEE, losses, bottleneck, verdict |
| `writer.py` | Claude drafts the owner alerts, with a template fallback |
| `notify.py` | Slack webhook + markdown copy |
| `run.py` | Daily runner, CLI and saved setup |
| `dashboard.py` | Streamlit dashboard with the line builder and both modes |

## Limits and next steps

- **The bottleneck is calculated, not observed.** Line mode compares each machine's demonstrated good output. To prove the bottleneck on the floor, the PLC should also log *blocked* (the next station is full) and *starved* (waiting for parts) states.
- **Parallel machines at one step**, for example 2 presses feeding 1 welder, are not modelled yet.
- **Out of scope:** predictive maintenance on the short-stop pattern, feeding true cycle times into a simulation model (DELMIA, Plant Simulation), load balancing across plants, and cloud deployment with a live PLC/SAP connection.
