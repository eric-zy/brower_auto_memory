export function runtimeLimits(env = process.env) {
  const wait = String(env.MIDSCENE_WAIT_FOR_MODEL ?? 'true').toLowerCase();
  if (!['true','false','1','0'].includes(wait)) throw Error('MIDSCENE_WAIT_FOR_MODEL必须为true或false');
  if (['true','1'].includes(wait)) return { requestMs: 0, navigationMs: 0 };
  function milliseconds(key, fallback) {
    if (env[key] !== undefined && !/^[0-9]+$/.test(String(env[key]).trim())) throw Error(`${key}必须是1000至600000的整数毫秒数`);
    const value = env[key] === undefined ? fallback : Number(env[key]);
    if (!Number.isInteger(value) || value < 1000 || value > 600000) throw Error(`${key}必须是1000至600000的整数毫秒数`);
    return value;
  }
  const requestMs = milliseconds('MIDSCENE_MODEL_TIMEOUT', 90000);
  const navigationMs = milliseconds('MIDSCENE_NAVIGATION_TIMEOUT', Math.max(180000, requestMs + 30000));
  if (navigationMs <= requestMs) throw Error('MIDSCENE_NAVIGATION_TIMEOUT必须大于MIDSCENE_MODEL_TIMEOUT');
  return { requestMs, navigationMs };
}

export function configureClientTimeout(client, requestMs) {
  client.timeout = requestMs;
  client.maxRetries = 0;
  // Midscene interprets 0 as unlimited; OpenAI 5.x otherwise schedules an
  // immediate abort for 0. Replace only its timer wrapper, preserving fetch,
  // caller AbortSignal, headers, streaming bodies and connection errors.
  if (requestMs === 0 && typeof client.fetchWithTimeout === 'function') {
    const boundedFetch = client.fetchWithTimeout.bind(client);
    client.fetchWithTimeout = async (url, init, ms, controller) => {
      if (ms !== 0) return boundedFetch(url, init, ms, controller);
      const { signal, ...options } = init || {};
      const abort = () => controller.abort(signal.reason);
      if (signal?.aborted) abort();
      else signal?.addEventListener('abort', abort, { once: true });
      const streaming = (globalThis.ReadableStream && options.body instanceof ReadableStream)
        || (options.body && typeof options.body === 'object' && Symbol.asyncIterator in options.body);
      try {
        return await client.fetch.call(undefined, url, {
          ...options, ...(streaming ? { duplex: 'half' } : {}), signal: controller.signal,
          ...(options.method ? { method: options.method.toUpperCase() } : {}),
        });
      } catch (error) {
        signal?.removeEventListener('abort', abort);
        throw error;
      }
    };
  }
  return client;
}
