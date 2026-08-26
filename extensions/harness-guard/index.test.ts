// El cableado: que el handler se enganche a `tool_call`, que mire el cwd real
// del proceso y que devuelva la forma que pi espera (`{ block, reason }`).
// Las reglas se testean aparte, en reglas.test.ts.
import { test } from "node:test";
import assert from "node:assert";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import montar from "./index.ts";

function piFalso() {
  const handlers: Record<string, Function> = {};
  return {
    pi: { on: (ev: string, fn: Function) => { handlers[ev] = fn; } } as any,
    llamar: (event: any) => handlers["tool_call"](event),
  };
}

/** Corre `fn` con el cwd del proceso puesto en `destino`, y lo deja como estaba.
 *
 *  El guard mira `process.cwd()`, así que estos tests dependen de dónde se
 *  corren. Y el gate se corre ADENTRO de un worktree del harness: un test que
 *  asuma "el cwd es el repo" pasa en la máquina del humano y rojea en todos los
 *  worktrees. Pasó: la noche del 2026-08-26 los ocho agentes se encontraron con
 *  este test en rojo y los ocho lo arreglaron por su cuenta, cada uno distinto. */
function enCwd(destino: string, limpiar: string, fn: () => void) {
  const antes = process.cwd();
  const estado = process.env.XDG_STATE_HOME;
  process.env.XDG_STATE_HOME = path.join(limpiar, "state");
  process.chdir(destino);
  try { fn(); } finally {
    process.chdir(antes);
    if (estado === undefined) delete process.env.XDG_STATE_HOME;
    else process.env.XDG_STATE_HOME = estado;
    fs.rmSync(limpiar, { recursive: true, force: true });
  }
}

/** Un worktree con la forma que arma `dispatch.worktree_path`. */
function enWorktree(fn: (wt: string) => void) {
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "guard-")));
  const wt = path.join(tmp, ".worktrees", "agent-harness-ticket-74");
  fs.mkdirSync(wt, { recursive: true });
  enCwd(wt, tmp, () => fn(wt));
}

/** Un directorio neutro: no es worktree del harness, corra donde corra el gate. */
function fueraDeWorktree(fn: () => void) {
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "guard-neutro-")));
  enCwd(tmp, tmp, fn);
}

test("bloquea con la forma que pi espera", () => {
  enWorktree(() => {
    const { pi, llamar } = piFalso();
    montar(pi);
    const r: any = llamar({ toolName: "bash", input: { command: "gh pr merge 92" } });
    assert.strictEqual(r.block, true);
    assert.match(r.reason, /harness-guard/);
    assert.match(r.reason, /merge/i);
  });
});

test("el trabajo del agente pasa sin ruido", () => {
  enWorktree(() => {
    const { pi, llamar } = piFalso();
    montar(pi);
    for (const c of ["git commit -m x", "git push -u origin ticket/74", "./scripts/gate.sh"]) {
      assert.strictEqual(llamar({ toolName: "bash", input: { command: c } }), undefined, c);
    }
  });
});

test("las tools que no escriben ni ejecutan ni se miran", () => {
  enWorktree(() => {
    const { pi, llamar } = piFalso();
    montar(pi);
    assert.strictEqual(llamar({ toolName: "read", input: { path: "/etc/passwd" } }), undefined);
  });
});

test("fuera de un worktree del harness no opina", () => {
  fueraDeWorktree(() => {
    const { pi, llamar } = piFalso();
    montar(pi);
    assert.strictEqual(llamar({ toolName: "bash", input: { command: "sudo rm -rf /" } }), undefined);
  });
});

test("cada bloqueo deja una linea en el log de eventos", () => {
  enWorktree(() => {
    const { pi, llamar } = piFalso();
    montar(pi);
    llamar({ toolName: "bash", input: { command: "git reset --hard HEAD~1" } });
    const log = path.join(process.env.XDG_STATE_HOME!, "harness", "events.jsonl");
    const linea = JSON.parse(fs.readFileSync(log, "utf8").trim().split("\n").pop()!);
    assert.strictEqual(linea.tipo, "guard");
    assert.strictEqual(linea.origen, "guard");
    assert.strictEqual(linea.ticket, "agent-harness#74");
    assert.match(linea.timestamp, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
    assert.match(linea.cuerpo, /git reset --hard/);
    // El esquema del EventLog de dispatch.py, entero: si falta una clave,
    // `harness status` lee una línea que no tiene la forma que espera.
    for (const k of ["timestamp", "contexto", "origen", "run_id", "ticket",
                     "attempt", "tipo", "ref", "cuerpo", "clase"]) {
      assert.ok(k in linea, `falta la clave ${k}`);
    }
  });
});
