// Las reglas del guard: qué puede correr un agente del harness adentro de su
// worktree. Cero imports de pi a propósito — acá vive todo el riesgo y todo
// se testea sin levantar un agente.
//
// El criterio no es "peligroso" sino "no es el trabajo del agente". El trabajo
// es: leer, escribir código, commitear, correr el gate, pushear su rama y abrir
// el PR. Todo lo que quede afuera de eso, y sea difícil de deshacer, se bloquea.
// `git commit` y `git push` NO se bloquean: sin eso el agente no existe.
import * as path from "node:path";

export type Veredicto = { motivo: string } | null;

const SHELLS = new Set(["sh", "bash", "zsh", "dash", "fish"]);
const DESCARGAS = new Set(["curl", "wget"]);
const APAGADO = new Set(["shutdown", "reboot", "halt", "poweroff"]);
// Un redirect a estos lados no es "escribir afuera": es scratch y basura.
const ESCRIBIBLES = ["/tmp", "/dev", "/private/tmp", "/var/folders"];

/** `.../.worktrees/<repo>-ticket-<n>` es lo que arma `dispatch.worktree_path`. */
const RUTA_WORKTREE = /\/\.worktrees\/([^/]+)-ticket-(\d+)(?:\/|$)/;

export function esWorktreeDelHarness(cwd: string): boolean {
  return RUTA_WORKTREE.test(cwd);
}

/** "agent-harness#74", el mismo formato que usa el log de eventos. */
export function ticketDe(cwd: string): string | null {
  const m = cwd.match(RUTA_WORKTREE);
  return m ? `${m[1]}#${m[2]}` : null;
}

type Tok = { t: string; op?: boolean };

/** Tokenizador chico que respeta comillas. Sin esto, un `git commit -m "no
 *  sudo"` se leería como un `sudo` y el agente quedaría trabado por su propio
 *  mensaje de commit. */
export function tokenizar(s: string): Tok[] {
  const out: Tok[] = [];
  let cur = "";
  let hay = false;
  const cerrar = () => {
    if (hay) { out.push({ t: cur }); cur = ""; hay = false; }
  };
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (c === "'" || c === '"') {
      const q = c;
      i++;
      while (i < s.length && s[i] !== q) { cur += s[i]; i++; }
      hay = true;
      continue;
    }
    if (c === "\\") { i++; if (i < s.length) { cur += s[i]; hay = true; } continue; }
    if (/\s/.test(c)) { cerrar(); continue; }
    const dos = s.slice(i, i + 2);
    if (dos === "&&" || dos === "||" || dos === ">>") {
      cerrar(); out.push({ t: dos, op: true }); i++; continue;
    }
    if (";|&><".includes(c)) { cerrar(); out.push({ t: c, op: true }); continue; }
    cur += c; hay = true;
  }
  cerrar();
  return out;
}

const CORTES = new Set(["&&", "||", ";", "|", "&"]);

function segmentos(toks: Tok[]): string[][] {
  const out: string[][] = [];
  let cur: string[] = [];
  for (const tk of toks) {
    if (tk.op) {
      if (CORTES.has(tk.t)) { if (cur.length) out.push(cur); cur = []; }
      continue;   // los redirects se miran aparte, no cortan el comando
    }
    cur.push(tk.t);
  }
  if (cur.length) out.push(cur);
  return out;
}

/** ¿La ruta se sale del worktree? La ruta relativa se resuelve contra `cwd`
 *  (que puede haberse movido con un `cd`) pero se compara siempre contra
 *  `raiz`, el worktree. `~` y `$VAR` cuentan como afuera: una variable sin
 *  expandir adentro de un `rm -rf` es el desastre clásico. */
export function fuera(p: string, cwd: string, raiz: string = cwd): boolean {
  if (!p) return false;
  if (p.startsWith("~") || p.startsWith("$")) return true;
  const abs = path.resolve(cwd, p);
  return abs !== raiz && !abs.startsWith(raiz + path.sep);
}

function esBandera(a: string): boolean {
  return a.startsWith("-") && a !== "-";
}

function sinBanderas(args: string[]): string[] {
  return args.filter((a) => !esBandera(a));
}

function tieneLetra(args: string[], letra: string): boolean {
  return args.some((a) => esBandera(a) && !a.startsWith("--") && a.includes(letra));
}

function analizarGit(resto: string[]): Veredicto {
  const sub = resto[0];
  const args = resto.slice(1);
  const libres = sinBanderas(args);

  if (sub === "merge") {
    return { motivo: "git merge: the dispatcher rebases your branch onto the base before dispatching you. Never merge inside a worktree — commit, push and open the PR." };
  }
  if (sub === "push") {
    if (args.some((a) => a === "-f" || a === "--force" || a === "--force-with-lease")) {
      return { motivo: "force-push rewrites history that other branches and the reviewer already saw. Push normally; if the branch diverged, stop and let the run fail." };
    }
    if (libres.some((a) => /^(HEAD:)?(main|master)$/.test(a))) {
      return { motivo: "pushing to main bypasses the pull request. Push your ticket branch and open the PR with 'Closes #N' — merging is the human's decision." };
    }
  }
  if (sub === "checkout" || sub === "switch") {
    const creando = args.some((a) => ["-b", "-B", "-c", "-C"].includes(a));
    if (!creando && /^(main|master)$/.test(libres[0] || "")) {
      return { motivo: "leaving your ticket branch: main is checked out elsewhere and moving it desyncs the main working copy. Stay on your branch." };
    }
  }
  if (sub === "worktree") {
    return { motivo: "worktrees belong to the dispatcher, not to the agent. It created yours and it will clean it up." };
  }
  if (sub === "branch" && (args.includes("-D") || args.includes("-d"))) {
    return { motivo: "deleting branches: a branch is the only thing that survives a failed attempt. Never delete one." };
  }
  if (sub === "reset" && args.includes("--hard")) {
    return { motivo: "git reset --hard throws away uncommitted work with no way back. Commit first, then decide." };
  }
  if (sub === "clean" && tieneLetra(args, "f")) {
    return { motivo: "git clean -f deletes untracked files, which is usually the code you just wrote and have not committed." };
  }
  if (sub === "reflog" && args[0] === "expire") {
    return { motivo: "expiring the reflog removes the only recovery path left after a mistake." };
  }
  if (sub === "gc" && args.some((a) => a.startsWith("--prune"))) {
    return { motivo: "pruning unreachable objects removes the only recovery path left after a mistake." };
  }
  return null;
}

function analizarGh(resto: string[]): Veredicto {
  if (resto[0] === "pr" && resto[1] === "merge") {
    return { motivo: "gh pr merge: the merge is the human's decision, always. Your job ends when the PR is open with a green gate." };
  }
  if (resto[0] === "repo" && resto[1] === "delete") {
    return { motivo: "deleting a repository is not recoverable and is never part of a ticket." };
  }
  if (resto[0] === "api") {
    const i = resto.findIndex((a) => a === "-X" || a === "--method");
    if (i >= 0 && (resto[i + 1] || "").toUpperCase() === "DELETE") {
      return { motivo: "a DELETE through the GitHub API destroys state outside this worktree. Nothing in a ticket needs it." };
    }
  }
  return null;
}

function analizarSegmento(seg: string[], cwd: string, raiz: string): Veredicto {
  const cmd = path.basename(seg[0] || "");
  const resto = seg.slice(1);

  if (cmd === "sudo") {
    return { motivo: "sudo: nothing a ticket needs lives outside your own permissions." };
  }
  if (APAGADO.has(cmd)) {
    return { motivo: `${cmd}: the machine is running other agents and the human's session.` };
  }
  if (cmd.startsWith("mkfs") || cmd === "wipefs" || cmd.startsWith("newfs_")) {
    return { motivo: `${cmd}: formatting a filesystem is never part of a ticket.` };
  }
  if (cmd === "diskutil" && ["erase", "zeroDisk", "secureErase", "reformat"].includes(resto[0])) {
    return { motivo: "destructive diskutil operation: never part of a ticket." };
  }
  if (cmd === "dd" && resto.some((a) => a.startsWith("of=/dev/"))) {
    return { motivo: "dd writing to a raw device destroys the disk. Never part of a ticket." };
  }
  if (cmd === "git") return analizarGit(resto);
  if (cmd === "gh") return analizarGh(resto);

  if (cmd === "rm") {
    const malo = sinBanderas(resto).find((a) => fuera(a, cwd, raiz));
    if (malo) {
      return { motivo: `rm on '${malo}', which is outside your worktree. You can only delete files under ${raiz}.` };
    }
  }
  if (cmd === "find" && resto.includes("-delete")) {
    const desde = sinBanderas(resto)[0] || ".";
    if (fuera(desde, cwd, raiz)) {
      return { motivo: `find -delete rooted at '${desde}', which is outside your worktree.` };
    }
  }
  return null;
}

function analizarRedirects(toks: Tok[], cwd: string): Veredicto {
  for (let i = 0; i < toks.length; i++) {
    if (!toks[i].op || (toks[i].t !== ">" && toks[i].t !== ">>")) continue;
    const destino = toks[i + 1];
    if (!destino || destino.op) continue;
    const p = destino.t;
    if (!fuera(p, cwd)) continue;
    if (p.startsWith("/") && ESCRIBIBLES.some((d) => p === d || p.startsWith(d + "/"))) continue;
    return { motivo: `redirecting output to '${p}', which is outside your worktree. Write only under ${cwd} (or /tmp).` };
  }
  return null;
}

// Una ruta que no existe en ningún lado: marca "me fui del worktree" cuando el
// `cd` no se puede resolver (`~`, `$VAR`). Todo lo relativo que venga después
// cae afuera, que es exactamente lo que queremos.
const AFUERA = "/__fuera_del_worktree__";

export function revisarBash(comando: string, cwd: string): Veredicto {
  const toks = tokenizar(comando);
  const segs = segmentos(toks);

  // curl … | sh se lee entero, no segmento por segmento.
  const cmds = segs.map((s) => path.basename(s[0] || ""));
  if (cmds.some((c) => DESCARGAS.has(c)) && cmds.some((c) => SHELLS.has(c))) {
    return { motivo: "piping a download into a shell runs code nobody reviewed. Download it, read it, then decide." };
  }

  const red = analizarRedirects(toks, cwd);
  if (red) return red;

  // `cd otro-lado && rm -rf algo` borra en otro lado: el cwd se arrastra de
  // segmento en segmento, si no las rutas relativas se miden contra el lugar
  // equivocado.
  let actual = cwd;
  for (const seg of segs) {
    if (path.basename(seg[0] || "") === "cd") {
      const destino = sinBanderas(seg.slice(1))[0];
      if (!destino) { actual = AFUERA; continue; }        // `cd` pelado = $HOME
      actual = destino.startsWith("~") || destino.startsWith("$")
        ? AFUERA : path.resolve(actual, destino);
      continue;
    }
    const v = analizarSegmento(seg, actual, cwd);
    if (v) return v;
  }
  return null;
}

export function revisar(tool: string, input: any, cwd: string): Veredicto {
  // Fuera de un worktree del harness esto es la sesión del humano: el guard
  // no opina. La extensión vive en ~/.pi/agent/extensions, que es global.
  if (!esWorktreeDelHarness(cwd)) return null;

  if (tool === "bash") return revisarBash(String(input?.command ?? ""), cwd);
  if (tool === "write" || tool === "edit") {
    const p = String(input?.path ?? "");
    if (fuera(p, cwd)) {
      return { motivo: `writing to '${p}', which is outside your worktree. Everything you change has to live under ${cwd} so it lands in your branch.` };
    }
  }
  return null;   // `read` afuera es legítimo: mirar el repo principal orienta.
}
