// Phone sounds, synthesized with the Web Audio API (no audio files needed).
const Sounds = (() => {
  let ctx = null;
  let loop = null; // { stop() } for the ringtone/ringback currently playing

  function context() {
    if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
    return ctx;
  }

  // Browsers only allow sound after the user interacts with the page.
  async function unlock() {
    const c = context();
    if (c.state !== "running") await c.resume();
    return c.state === "running";
  }

  function unlocked() {
    return !!ctx && ctx.state === "running";
  }

  // Play a chord of frequencies from `start` for `dur` seconds.
  function tone(freqs, start, dur, gain = 0.12) {
    const c = context();
    const g = c.createGain();
    g.gain.setValueAtTime(0, start);
    g.gain.linearRampToValueAtTime(gain, start + 0.02);
    g.gain.setValueAtTime(gain, start + dur - 0.03);
    g.gain.linearRampToValueAtTime(0, start + dur);
    g.connect(c.destination);
    for (const f of freqs) {
      const o = c.createOscillator();
      o.frequency.value = f;
      o.connect(g);
      o.start(start);
      o.stop(start + dur);
    }
  }

  // Repeat `pattern(t0)` every `period` seconds until stopped.
  function repeat(pattern, period) {
    stop();
    const c = context();
    let next = c.currentTime + 0.05;
    let timer = null;
    const schedule = () => {
      while (next < c.currentTime + 1) {
        pattern(next);
        next += period;
      }
      timer = setTimeout(schedule, 250);
    };
    schedule();
    loop = { stop: () => clearTimeout(timer) };
  }

  function stop() {
    if (loop) loop.stop();
    loop = null;
  }

  return {
    unlock,
    unlocked,
    stop,
    // Incoming call: bright double ring, every 3 s.
    ringtone() {
      repeat((t) => {
        for (let i = 0; i < 2; i++) {
          const s = t + i * 0.5;
          for (let k = 0; k < 8; k++) tone([k % 2 ? 1320 : 1760], s + k * 0.05, 0.05, 0.09);
        }
      }, 3);
    },
    // Outgoing call: US-style ringback, 2 s on / 4 s off.
    ringback() {
      repeat((t) => tone([440, 480], t, 2, 0.08), 6);
    },
    connected() {
      stop();
      const t = context().currentTime + 0.02;
      tone([660], t, 0.09);
      tone([990], t + 0.11, 0.12);
    },
    hangup() {
      stop();
      const t = context().currentTime + 0.02;
      [620, 520, 420].forEach((f, i) => tone([f], t + i * 0.18, 0.14));
    },
  };
})();
