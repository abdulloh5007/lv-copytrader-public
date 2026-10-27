/* Two rigid arm segments: rotation changes the pose without stretching joints. */
export function armPose(shoulder, upperLength, lowerLength, upperAngle, lowerAngle) {
  const rad = Math.PI / 180;
  const elbow = [shoulder[0] + Math.cos(upperAngle * rad) * upperLength,
                 shoulder[1] + Math.sin(upperAngle * rad) * upperLength];
  const hand = [elbow[0] + Math.cos(lowerAngle * rad) * lowerLength,
                elbow[1] + Math.sin(lowerAngle * rad) * lowerLength];
  return { elbow, hand };
}
