"""Operator admin app (design section 7.12).

The web replacement for running operator commands on a pod. The standardization team signs
in through OpenShift's oauth-proxy and can see each team's schedule, open any run's full
scorecard, publish or withdraw a week, and record decisions on findings. Every write is made
under the signed-in person's identity.

* :mod:`~alerts_bi_admin.app` - routes, the identity requirement and security headers
* :mod:`~alerts_bi_admin.auth` - who is signed in, and the anti-forgery token on every form
* :mod:`~alerts_bi_admin.queries` - what it reads, with the owning credential
* :mod:`~alerts_bi_admin.pages` - its server-rendered HTML

It is a separate application from the reader portal and the trigger surface. It binds to
loopback only, so the login proxy in the same pod is the only thing that can reach it.
"""
