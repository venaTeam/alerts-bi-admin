# What Is an Incorrect Alert?

**Created by:** Eden Fried  
**Last modified on:** Mar 26, 2026  

---

The purpose of this document is to formally define what a "bad" alert is.  
The document is intended to serve development, operations, and monitoring teams in maintaining a standard for proper alerting, and to ensure that alerts reflect only real problems, call for action, and support the accompanying operational processes.

---

## What Is a Bad Alert?

A "bad" alert is any alert that fails to communicate — clearly, accurately, and reliably — a **real operational problem** that requires investigation, action, or a decision.

An alert is considered **"bad"** when it:
* Creates ambiguity or confusion
* Does not call for a clear action (investigation, fix, additional attention, escalation)
* Is not based on objective data

---

## Key Characteristics of a Bad Alert

### 1. Lack of Context and Clarity — An alert must enable immediate understanding of the event

* **A bad alert will contain:**
  * An incorrect / unclear component or system name
  * Missing details about the type of failure (what actually happened)
  * Missing explanation of the environment in which the alert was triggered
* **A good alert should quickly answer 3 questions:**
  1. What happened?
  2. Where did it happen?
  3. What is the impact?

* **Example of a bad alert:** `message: "Unable to get data"`
* **Example of a good alert:** `message: "cpu in pod X usage is 90%", impact: "higher latency"`

---

### 2. Non-Actionable Alerts

An alert that does not lead to a clear action (investigation, fix, additional attention, escalation) does not meet the standard.

Informational-only alerts — an alert that tells me about an event that occurred in the system with no impact should not be sent through the alerting mechanism — **that's not an alert, that's a log!**

* **Example of a bad alert:** `message: "i am alive"`
* **Example of a good alert:** `message: "storage in VM X is full"`

---

### 3. Missing Severity or Impact Definition

An alert without a severity classification (Critical / High / Warning) creates confusion. The absence of severity harms the ability to prioritize and respond. The alert's severity should be derived from its **impact**, not from its technical description.

* **Example of a bad alert:** `impact: "high cpu"`
* **Example of a good alert:** `impact: "higher latency"`

---

### 4. Unrepresentative or Misleading Metadata — Accuracy in fields such as Node, Application, Operator

Using generic values such as `Unknown`, `Test`, or `Default` harms the clarity of the alert and the value it is supposed to provide.

---

### 5. Non-Indicative Content — The alert details must explain the failure, not just the outcome

* **Example of a bad alert:** `Error Occurred`
* **Example of a good alert:** `Payment service timeout exceeded 5 seconds for 30% of requests`

---

### 6. Alerts Not Based on Metrics

A good alert is based on metrics that enable tracking, investigation, and identification (RCA — Root Cause Analysis).

* **Example of a bad alert:** An alert triggered from the code only, not sent based on metrics that track the system's state.
* **Example of a good alert:** A metrics-based alert created by Grafana Alert Rules, linked to a panel that allows the operator to jump into investigation quickly, reliably, and accurately. Coming soon — support for Alert Rules defined in Prometheus & Victoria Alert Managers.

---

### 7. Invalid Timestamp — A wrong or missing timestamp harms the ability to properly investigate the event; time synchronization between systems must be ensured

An alert that arrives in the pipeline without a timestamp representing when the alert actually occurred causes a lack of clarity and confusion in handling the alert, and makes it difficult to determine whether the alert is still relevant.

---

## Incorrect Uses of Alerts — DON'T DOs

The following uses are not just incorrect — they are practices you **must not do**:

* ❌ **DON'T send Info Alerts:** Alerts that "tell" us about an event that occurred in the system or report on process status. These belong in logs, not alerts.
* ❌ **DON'T send Heartbeat alerts:** The purpose of an alert is to indicate a problem that needs attention; sending an alert to say "everything is fine" contradicts that purpose.

---

### So How Can I Still Monitor Health?

#### Health Monitoring Without Sending "I'm Alive" Alerts

**UP metrics:**

`up{job="<job-name>", instance="<instance-id>"}`

This might be the most useful metric you'll ever come across! If the instance is functioning properly — exposing metrics and reachable, for example by Prometheus — the metric value will be 1. When the scrape fails, its value will be 0.

We can define an Alert Rule that checks the metric value, and when it drops below 1, the alert fires!  
This way we monitor health using metrics, not alerts.

---

> ⚠️ **Alert quality directly affects system stability and operational efficiency. An abundance of non-critical alerts harms the detection of real incidents and leads to burnout and indifference — "Alert Fatigue" (the boy who cried wolf).**  
> **Proper alert management is a critical component of operating modern systems.**

---

## Customer Compass — Guidance for Getting Started

To help you identify your alerts that do not meet the standard, we created the **"Alert Standard Evaluation"** dashboard.

The dashboard is divided into 2 sections:

1. **The upper section of the dashboard** presents a snapshot of the alerts you are sending, according to the Operator selected as a Variable. This section is intended to help you **track the alerts you send and their frequency**:
   * The number of alerts sent
   * The alerts themselves
   * The number of alerts sent per hour
   * The alert rules in use
   * Whether they are sent via Grafana Alert Rule / API (regarding the number of alerts you send and their frequency, keep in mind that alerts sent via a Grafana alert rule are sent every 5 minutes).

2. **The lower section (Examples):** presents examples of alerts that do not meet the standard, according to the characteristics described in this document. This section is intended to **guide you in improving your alerts**, and to help find specific, problematic cases.

---

> ℹ️ **Please note!**  
> The examples are captured in a literal way, based on keywords, and do not cover all use cases. The dashboard is intended to **guide you as you enter the alert-improvement process**.
