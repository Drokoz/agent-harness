// harness-guard — el cableado. Toda la decisión vive en `reglas.ts`; acá sólo
// se engancha al evento `tool_call` de pi, que puede bloquear la llamada antes
// de que se ejecute.
//
// Instalación:
//   cp -r extensions/harness-guard ~/.pi/agent/extensions/
//   (pi lo autodescubre; `/reload` en una sesión abierta)
//
// No tiene dependencias npm a propósito: nada que instalar, nada que se rompa
// a las 3 de la mañana. El único import de pi es de tipos, que desaparece en
// runtime, así que `reglas.ts` y este archivo se pueden testear con
// `node --experimental-strip-types` sin resolver el paquete de pi.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { esWorktreeDelHarness, revisar, ticketDe } from "./reglas.ts";

const VIGILADAS = new Set(["bash", "write", "edit"]);

function estadoDir(): string {
  const xdg = process.env.XDG_STATE_HOME;
  return xdg ? path.join(xdg, "harness")
             : path.join(os.homedir(), ".local", "state", "harness");
}

/** Una línea en el mismo log que escribe el dispatcher, con el mismo esquema.
 *  Un bloqueo que no deja rastro es un bloqueo que a la mañana no existió. */
function anotar(cwd: string, tool: string, detalle: string, motivo: string): void {
  try {
    const dir = estadoDir();
    fs.mkdirSync(dir, { recursive: true });
    const linea = {
      timestamp: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
      contexto: "harness",
      origen: "guard",
      run_id: null,
      ticket: ticketDe(cwd),
      attempt: null,
      tipo: "guard",
      ref: tool,
      cuerpo: `${detalle.slice(0, 300)} -- ${motivo}`,
      clase: "modelo",
    };
    fs.appendFileSync(path.join(dir, "events.jsonl"),
                      JSON.stringify(linea) + "\n", "utf8");
  } catch {
    // El guard nunca puede tumbar al agente: si no puede loguear, igual bloquea.
  }
}

function detalleDe(tool: string, input: any): string {
  if (tool === "bash") return String(input?.command ?? "");
  return String(input?.path ?? "");
}

export default function (pi: ExtensionAPI): void {
  pi.on("tool_call", (event: any) => {
    const tool = String(event?.toolName ?? "");
    if (!VIGILADAS.has(tool)) return;

    // El cwd del proceso de pi es el worktree: lo fija `herdr pane split --cwd`
    // y un `cd` adentro de un bash no lo mueve.
    const cwd = process.cwd();
    if (!esWorktreeDelHarness(cwd)) return;

    const v = revisar(tool, event.input, cwd);
    if (!v) return;

    const detalle = detalleDe(tool, event.input);
    anotar(cwd, tool, detalle, v.motivo);
    process.stderr.write(`harness-guard: bloqueado ${tool}: ${v.motivo}\n`);
    return {
      block: true,
      reason: `Blocked by harness-guard: ${v.motivo}\n\n` +
        "This is an unattended run. Do not try to work around the block: " +
        "finish the ticket the normal way — commit, run ./scripts/gate.sh, " +
        "push your branch and open the PR.",
    };
  });
}
