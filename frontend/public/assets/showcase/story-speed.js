/* Split a frame at trade completion so post-trade animation runs at normal speed. */
export function advanceStoryTime(time, elapsed, accelerating, finishTime) {
  if (!accelerating) return { advance: elapsed, stop: false };
  if (finishTime == null || !Number.isFinite(finishTime)) return { advance: elapsed * 4, stop: false };
  const remaining = Math.max(0, finishTime - time);
  const fastSeconds = remaining / 4;
  if (elapsed < fastSeconds) return { advance: elapsed * 4, stop: false };
  return { advance: remaining + Math.max(0, elapsed - fastSeconds), stop: true };
}
