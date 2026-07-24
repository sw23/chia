OpenShell Agent Nodes
=====================

CHIA can run a coding agent (and, optionally, the untrusted commands it
generates) inside an `NVIDIA OpenShell <https://github.com/NVIDIA/OpenShell>`_
sandbox — a policy-governed runtime that constrains an agent's filesystem,
network, process, and inference access through declarative YAML policies.

This is an **opt-in execution mode**. By default CHIA agent backends run the
agent CLI as a local subprocess on the Ray worker (unchanged behavior); enabling
OpenShell swaps only the *execution transport*.

.. contents::
   :local:
   :depth: 2

Why
---

The agent backends (:class:`~chia.models.copilot.CopilotLLM`,
:class:`~chia.models.claude.ClaudeCodeLLM`) historically shelled out to their CLI
directly on the worker, and untrusted tool commands ran through a bash/sim tool
with (at most) an ``argv[0]`` allow-list. OpenShell adds defense-in-depth around
the agent: egress is denied by default and opened with a short policy the proxy
enforces at the HTTP method/path level, credentials are injected as environment
variables rather than mounted onto the sandbox filesystem, and the filesystem and
process domains are constrained at sandbox creation.

Architecture
------------

The integration introduces a small execution-transport seam,
:class:`~chia.base.sandbox_runner.SandboxRunner`, that both agent backends
delegate their subprocess call to:

.. code-block:: text

   CopilotLLM / ClaudeCodeLLM ._build_cmd()
           -> SandboxRunner
                |-- LocalSubprocessRunner   (default; today's behavior)
                |-- OpenShellRunner         (opt-in; runs cmd inside a sandbox)

- :class:`~chia.base.sandbox_runner.LocalSubprocessRunner` reproduces the prior
  behavior exactly and remains the default.
- :class:`~chia.models.openshell.OpenShellRunner` creates (or reuses) an
  OpenShell sandbox via the ``openshell`` CLI and runs each agent turn inside it.

Prerequisites
-------------

- The ``openshell`` CLI installed on the worker and a reachable OpenShell
  gateway with a configured compute driver. See the
  `OpenShell quickstart <https://docs.nvidia.com/openshell/get-started/quickstart>`_.
- **Topology:** prefer a bare/VM agent worker so OpenShell owns the single
  container layer. Nesting an OpenShell sandbox inside CHIA's own Docker worker
  means Docker-in-Docker; avoid it unless you deliberately wire a host gateway
  socket into the worker.

Configuration
-------------

:class:`~chia.models.openshell.OpenShellConfig` declares how sandboxes are
created:

.. code-block:: python

   from chia.models.openshell import OpenShellConfig, OpenShellRunner
   from chia.models.copilot import CopilotLLM

   cfg = OpenShellConfig(
       sandbox_from="base",                 # --from <image|dir|catalog>
       providers={"GITHUB_TOKEN": "github"},# env var -> OpenShell provider type
       policy="lockdown-policy.yaml",       # base policy path, or an inline dict
       reuse_sandbox=True,                  # create once, exec per turn
   )
   llm = CopilotLLM(model="...", runner=OpenShellRunner(cfg))

Network policy for MCP tools
----------------------------

A sandboxed agent must still reach CHIA's MCP tool servers
(``http://{hostname}:{port}/{name}/mcp``). When the base ``policy`` is a dict (or
``None``), :func:`~chia.models.openshell.generate_mcp_egress_policy` appends one
egress allow entry per live tool endpoint, merged onto your base policy, so tool
calls are not silently blocked. Generate the allow-list from the *live* tool
registry — never hand-maintain it.

Using it in the Mantis RTL loop
-------------------------------

The ``examples/mantis_rtl_loop`` example is wired for this. Flip the mode via the
``SANDBOX`` knob (or the ``MANTIS_SANDBOX`` environment variable) in
``examples/mantis_rtl_loop/config.py``:

.. code-block:: python

   SANDBOX = "openshell"        # default "local"
   OPENSHELL = {
       "sandbox_from": None,    # e.g. "base" or a custom image with copilot+iverilog
       "providers": {"GITHUB_TOKEN": "github"},
       "policy": "lockdown-policy.yaml",
       "gpu": False,
       "reuse_sandbox": True,
   }

A generic locked-down base policy (default-deny network, design directory only)
lives at ``examples/mantis_rtl_loop/lockdown-policy.yaml`` and works for any
design under review.

Untrusted tool execution
-------------------------

Independently of OpenShell, ``SimTool`` (which runs AI-generated
testbenches/formal harnesses) executes its allow-listed program **without a
shell**, so shell operators (``&&``, ``|``, ``;``, ``$(...)``) are not
interpreted and the ``argv[0]`` allow-list is enforced. ``BashTool``
remains a general shell by design; its isolation comes from the surrounding
container/sandbox, not an allow-list.

Status
------

OpenShell is alpha software; keep ``LocalSubprocessRunner`` as the default and a
supported fallback. The live-gateway tests are gated behind an
``OPENSHELL_GATEWAY`` skip so the suite runs without a gateway.
