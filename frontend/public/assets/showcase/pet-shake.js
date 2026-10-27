/* Detect deliberate back-and-forth movement without mistaking a normal drag for a shake. */
export function createShakeDetector(x, y, time) {
  let anchor = { x, y };
  let segments = [];
  let triggered = false;
  return {
    sample(nextX, nextY, now) {
      if (triggered) return true;
      const dx = nextX - anchor.x, dy = nextY - anchor.y, distance = Math.hypot(dx, dy);
      if (distance < 10) return false;
      anchor = { x: nextX, y: nextY };
      segments.push({ dx, dy, distance, time: now });
      segments = segments.filter(segment => now - segment.time <= 1400);
      let reversals = 0, travel = 0;
      for (let i = 0; i < segments.length; i++) {
        const current = segments[i];
        travel += current.distance;
        if (i) {
          const previous = segments[i - 1];
          const alignment = (previous.dx * current.dx + previous.dy * current.dy) / (previous.distance * current.distance);
          if (alignment < -.5) reversals++;
        }
      }
      triggered = reversals >= 3 && travel >= 105;
      return triggered;
    },
  };
}
