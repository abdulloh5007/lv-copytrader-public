export class SignupError extends Error {
  constructor(
    public status: number,
    public retryAfter: number = 60,
  ) {
    super('Signup failed');
  }
}

export async function joinWaitlist(
  email: string,
  consent: boolean,
  website: string,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch('/api/waitlist', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ email, consent, website }),
    signal,
    credentials: 'same-origin',
  });
  if (!response.ok)
    throw new SignupError(
      response.status,
      Number(response.headers.get('Retry-After')) || 60,
    );
  const body = await response.json();
  if (body.ok !== true) throw new Error('Invalid signup response');
}
