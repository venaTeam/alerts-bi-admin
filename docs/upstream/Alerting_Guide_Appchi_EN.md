# The Guide to Proper Alerting: Monitoring & Management Doctrine in Appchi

## Purpose of This Document
1. **Define the new operational standard we are now leading in the unit-wide alerting domain** — we are making a sharp transition from "noisy" monitoring, in which the pipeline is flooded with junk alerts and suspensions, to **surgical, reliable, and focused monitoring**.
2. **Detail the full process for creating an alert in the central alerting pipeline**: from creation and definition in code/Grafana, through its technical precision and detailing, to becoming operational and handing over operational responsibility to the shift teams.
3. **Align areas of responsibility between the development teams (LVL3) and the operations teams (LVL2)**, and ensure that every alert is a reliable indication that enables an accurate response and drives immediate action.

---

## What Is an Alert?
An alert is a message intended for human eyes, indicating that **the system is faulty or is at risk of imminent failure**.

Beyond being a technical data point, an alert is genuinely a **call for help**, and therefore must justify interrupting the workflow (or sleep) of the person handling it in order to perform urgent mitigation (corrective action).

**A high-quality alert focuses on symptoms that affect the user experience or system operability, must be actionable, and requires human intervention.**

---

## What Should I Base an Alert On?

### Google's Four "Golden Signals"
Based on Google's article *"Monitoring Distributed Systems"*, every alert should be based on the following 4 metrics:

1. **Latency:** The component's response time.
2. **Traffic:** Load and demand.
3. **Errors:** Failed operations.
4. **Saturation:** Proximity to capacity limits (for predicting failure).

These metrics are the most essential for monitoring distributed systems and have been established as a standard to indicate service health, user experience, and component operability, while keeping things simple and preventing unnecessary "monitoring noise."

---

## How Do I Trigger an Alert?
The central channel for sending alerts is via **Alert Rules in Grafana**, based on metrics from the relevant components.

This is the primary and recommended channel because it enables:

* **Threshold-based precision:** Setting a deviation threshold based on the four golden signals.
* **Immediate visual context:** Every alert is automatically linked to a graph showing the historical trend, allowing the on-call soldier to make better real-time decisions about the next step and to shorten incident handling time.
* **Flexibility and fast maintenance:** Changing and editing the alert logic of a Grafana Alert Rule is faster and simpler than making a code change and redeploying.

---

## Defining Severity

### Which Severity Level Fits My Alert?
There are 3 severity levels: **Warning, High, Critical**

Choosing the severity level is the single most important factor in preventing alert fatigue and maintaining operational readiness.

Below we define when each level should be used, in order to ensure an accurate response.

### 1. Critical — Immediate Escalation
This is the level meant to be loud on the incident dashboard, **paging the on-call soldier at any hour, including the middle of the night**.

**When to use it?**
When there is a fault that directly harms the end user or the mission.

**Mandatory condition: the alert must pass the Wake-Up Test.**

**Examples:**

1. A component instance going down and a resulting degradation of service.
2. High latency that harms data quality or credibility.

#### The Wake-Up Test
Before defining a Critical-severity alert, you must be able to answer "yes" to all 3 of the following questions:

* a. **Is it urgent?** (Must it be handled now — it can't wait until morning?)
* b. **Does it cause immediate damage?** (Is the end user or the mission harmed?)
* c. **Is it actionable?** (Has a link to a runbook been included in the alert body?)

### 2. High — Top Priority
A severity level that alerts the operations team and warrants immediate attention and handling during working hours.

**When to use it?**
For a serious event that requires a rapid response, but does not necessarily justify waking a team in the middle of the night.

**Example:**
An alert about high saturation of system resources that will lead to a system crash within hours.

### 3. Warning — Tracking Only
A severity level that raises an alert and warrants follow-up.

**When to use it?**
For an event that requires monitoring and could deteriorate into an event that demands immediate intervention.

**Example:**
An alert about saturation in a non-critical system that is not expected to develop into a complex incident, but could grow over time and therefore should be monitored and tracked.

---

## Defining Impact

### Message vs. Impact
Conceptually, when creating a new alert we want to distinguish between the technical cause and the operational symptom.

* **Impact (the operational symptom):** Describes how the user is affected (for example: "slowness in the pipeline"). **This is the most critical field for the operations team.**
* **Message (the technical cause):** Describes why it happened (for example: "CPU at 95%"). Helps the operations team handle the fault.

Creating an accurate Impact field in a distributed system, or in an infrastructure-serving technology, is a complex challenge: infrastructure providers deliver cross-cutting services without necessarily knowing the exact operation that the infrastructure enables.

Despite this complexity, defining the operational Impact is **essential and mission-critical**: it is what allows the on-call soldier to triage in real time among dozens of simultaneous events and decide with confidence on the next steps (whether to wake on-call staff from additional teams, whether to call more people in to the site).

**Without a clear Impact, an alert remains a dry technical data point that does not drive action — inevitably increasing response time (MTTR — Mean Time To Repair) and burning out the operating teams.**

---

## Guiding Principles for Creating a Runbook

**Purpose of the runbook:** To enable operations personnel to understand what the problem is, what to do, and when to escalate.

### Recommended Structure
1. **Short description + impact:** "High latency in Kafka causing delays in alert delivery to users" — in most cases the description and the Impact will be similar to what is written in the alert the runbook is attached to.
2. **Links to relevant dashboards:** Investigation dashboards in Grafana.
3. **Initial checks to perform** (a fixed checklist of sorts): For example — Is the service up? Is there abnormal load?
4. **Diagnosis and investigation:** Deeper checks (logs, investigative metrics). It's important to write down what to look for (an overloaded service, a heavy request, etc.).
5. **Suggested remedies:** For example — restart service X.
6. **Escalation:** If the surface-level checks don't resolve the issue, who to turn to (which team the fault is handed off to for further investigation).

### Additional Important Principles
1. **Write for someone who just woke up in the middle of the night:** Short, clear, and sharp.
2. **Don't overload with text and checks:** Use links where the answer can be found quickly (a dashboard with metric-based information, logs).
3. **Continuous updating:** Update whenever there are new use cases worth checking, streamline it if you find checks that are less relevant, etc.
4. **Departmental standard:** Maintain a uniform structure.

> **Note:** The runbook will likely differ from team to team according to its scope and past experience.
>
> **The primary goal of the runbook is to minimize the "wait, where do I even start?" moment** and to lay out a clear path for beginning investigation and work.
>
> **In addition:** It is recommended to hold a monthly meeting with the LVL2 team (where one exists) to understand what they found missing in the runbook, where they got stuck investigating faults, what was less relevant and wasted their time — and to learn as you go in order to reach the best results.

---

## Alert Lifecycle Doctrine
The technical process for adding alerts to the operational monitoring pipeline, with responsibility divided between the development team (LVL3) and the shift team (LVL2).

### Stage A: Development and Alert Creation (LVL3 Responsibility)
The development team defines the logic that will detect system anomalies and characterizes the alert based on the metrics defined throughout this guide:

* **Focus on symptoms:** Alerts should be defined based on the user experience and the damage caused to the user (for example: slowness or visible errors), not solely on internal technical causes.
* **Impact accuracy:** The developer is obligated to precisely define the urgency level and the impact of the fault on the operational mission.
* **Runbook creation:** An alert is not complete without clear instructions for action. If the required response is entirely "robotic," LVL3 must automate the process rather than generate a human-facing alert. If an alert doesn't require a response, it isn't an alert.

**Using the "Four Golden Signals":** Alert definition will be based on latency, traffic, errors, or saturation metrics.

### Stage B: Review and Operational Approval (LVL2 Responsibility)
The shift team serves as the professional gatekeeper for the alerting pipeline, since they are the ones who handle the incident in real time.

The shift team must verify that the alert meets the professional standards that enable efficient and agile work.

1. **Actionability test:** The shift team will approve adding an alert only if it genuinely requires immediate corrective action and includes a link to a clear runbook.
2. **Preventing operator burnout (alert fatigue):** The operational level will reject alerts defined as "noise," or those that do not require an urgent response, to ensure that meaningful alerts receive the alertness they deserve.
3. **Threshold approval:** LVL2 verifies that the thresholds set by LVL3 are neither too sensitive (to prevent alert fatigue) nor too lenient (to prevent monitoring blindness).
4. **Promotion to the operational pipeline:** Only after LVL2 approval will the alert be permitted to enter the operational pipeline and surface on the shift dashboard.

### Ongoing Operation, Maintenance, and Retirement (Lifecycle Maintenance)
Alert relevance must be verified over time.

This is a significant stage in the process, meant to prevent flooding the pipeline with monitoring noise — which leads to active filtering of alerts out of the dashboard, long and unclear queries, and missed reliable operational indications.

**The mandatory principles are:**

* **Periodic review:** Alerts that remain in the same status for an extended period will be reviewed for removal from the repository.
* **Retirement at project closure:** When a system is decommissioned, the LVL3 team is obligated to remove all monitoring rules and alerts associated with it, to prevent a "graveyard" of orphaned alerts.
* **Fixing irrelevant monitoring:** An SLA will be defined between the operational levels that initiates a "monitoring fix" process after an LVL2 team escalation. Upon examining a claim that the monitoring is incorrect or irrelevant, the LVL3 team must commit to a defined timeframe for refining and tuning the alert, or for removing it.
