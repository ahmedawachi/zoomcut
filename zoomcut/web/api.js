// Talking to the local Zoomcut server.

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

export async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
  let r;
  try {
    r = await fetch(path, opt);
  } catch {
    throw new ApiError('Zoomcut is not running any more. Start it again with `zoomcut`.', 0);
  }
  const j = await r.json().catch(() => ({ error: `Zoomcut sent something unreadable (HTTP ${r.status})` }));
  if (!r.ok || j.error) throw new ApiError(j.error || `HTTP ${r.status}`, r.status);
  return j;
}

export const fileUrl = p => '/api/file?path=' + encodeURIComponent(p);
export const posterUrl = p => '/api/poster?path=' + encodeURIComponent(p);

export function wallpaperUrl(name, w = 256, h = 160) {
  return `/api/wallpaper-thumb?name=${encodeURIComponent(name)}&w=${w}&h=${h}`;
}

/** Send a file's bytes to the server - a browser never reveals a dropped
 *  file's path, so this is the only way drag and drop can really work. */
export function upload(file, onProgress) {
  const xhr = new XMLHttpRequest();
  const promise = new Promise((resolve, reject) => {
    xhr.open('POST', '/api/upload?name=' + encodeURIComponent(file.name));
    xhr.setRequestHeader('X-Zoomcut-Upload', '1');
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = e => e.lengthComputable && onProgress?.(e.loaded / e.total);
    xhr.onload = () => {
      let j = {};
      try { j = JSON.parse(xhr.responseText || '{}'); } catch { /* reported below */ }
      if (xhr.status >= 200 && xhr.status < 300 && !j.error) resolve(j);
      else reject(new ApiError(j.error || `upload failed (HTTP ${xhr.status})`, xhr.status));
    };
    xhr.onerror = () => reject(new ApiError('the upload was interrupted', 0));
    xhr.onabort = () => reject(new ApiError('upload cancelled', 0));
    xhr.send(file);
  });
  return { promise, abort: () => xhr.abort() };
}
