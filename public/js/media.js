/* global window, api, kit, ui, document, FormData, fetch */
/**
 * Media library (2.3).
 *
 * Uploads go through fetch directly rather than the api wrapper: the
 * wrapper JSON-encodes bodies, and a multipart upload must not be.
 * The CSRF header still travels, because that is what the API checks.
 */
(function () {
  'use strict';

  const { h, mount, toast, table, panel, panelBody, toolbar, search, badge, select,
    formDrawer, confirmButton, actionButton, pager, notice, tabs, bytes,
    textInput, textarea, formatDate, relativeTime, openDrawer } = kit;

  async function media(ctx) {
    const trashed = ctx.params.view === 'trash';
    const page = Number(ctx.params.page) || 1;

    mount(ctx.el, ui.spinner());
    const [data, folders, tags] = await Promise.all([
      api.get(`/api/media${api.qs({
        q: ctx.params.q, folder_id: ctx.params.folder_id, mime: ctx.params.mime,
        tag: ctx.params.tag, unused: ctx.params.unused, trashed: trashed || '', page,
      })}`),
      api.get('/api/media/folders/list'),
      api.get('/api/media/tags'),
    ]);

    ctx.setHead('Media',
      `${data.library.files} file(s) · ${bytes(data.library.bytes)} · ${data.storage.backend} storage`,
      uploadButton(ctx));

    const dropzone = h('div.dropzone', {}, [
      h('strong', { text: 'Drop files here to upload' }),
      h('span.muted', {
        text: 'Images get WebP and AVIF variants at responsive widths automatically.',
      }),
    ]);
    wireDropzone(ctx, dropzone);

    const grid = data.media.length
      ? h('div.media-grid', {}, data.media.map((m) => tile(ctx, m)))
      : panelBody(h('p.muted', {
        text: trashed ? 'The media trash is empty.' : 'No files match. Upload something to start.',
      }));

    mount(ctx.el, [
      tabs(ctx, [['', 'Library', data.library.files], ['trash', 'Trash']],
        ctx.params.view || '', 'view'),
      data.storage.backend === 'local'
        ? notice('Media is stored on this server’s disk. Set MEDIA_STORAGE=s3 before '
          + 'deploying to Fargate — a container filesystem does not survive a restart.', 'warn')
        : null,
      panel(null, [
        toolbar([
          search(ctx.params.q, (q) =>
            ctx.navigate(`#/media${api.qs({ ...ctx.params, q, page: 1 })}`)),
          select([['', 'All folders'], ['0', 'Unfiled'],
            ...folders.folders.map((f) => [f.id, `${f.name} (${f.file_count})`])],
          ctx.params.folder_id || '',
          (e) => ctx.navigate(`#/media${api.qs({ ...ctx.params, folder_id: e.target.value, page: 1 })}`)),
          select([['', 'All types'], ['image', 'Images'], ['video', 'Video'],
            ['application/pdf', 'PDF'], ['text', 'Text']], ctx.params.mime || '',
          (e) => ctx.navigate(`#/media${api.qs({ ...ctx.params, mime: e.target.value, page: 1 })}`)),
          select([['', 'All tags'], ...tags.tags.map((t) => [t.tag, `${t.tag} (${t.n})`])],
            ctx.params.tag || '',
            (e) => ctx.navigate(`#/media${api.qs({ ...ctx.params, tag: e.target.value, page: 1 })}`)),
          h('div.spacer'),
          h('button.btn.btn-sm', {
            type: 'button',
            text: ctx.params.unused ? 'Showing unused' : 'Show unused only',
            onclick: () => ctx.navigate(`#/media${api.qs({
              ...ctx.params, unused: ctx.params.unused ? '' : '1', page: 1,
            })}`),
          }),
          h('button.btn.btn-sm', {
            type: 'button', text: 'Folders',
            onclick: () => manageFolders(ctx, folders.folders),
          }),
        ]),
        trashed ? null : dropzone,
        grid,
        pager(ctx, data.page, data.pages),
      ]),
    ]);
  }

  function tile(ctx, m) {
    const preview = m.isImage
      ? h('img', { src: m.url, alt: m.alt_text || '', loading: 'lazy' })
      : h('div.media-icon', { text: (m.mime_type.split('/')[1] || 'file').toUpperCase() });

    return h('button.media-tile', {
      type: 'button',
      onclick: () => openFile(ctx, m.id),
      title: m.original_filename,
    }, [
      h('div.media-thumb', {}, preview),
      h('div.media-meta', {}, [
        h('span.cell-name', { text: m.original_filename }),
        h('span.cell-meta', {
          text: [
            bytes(m.byte_size),
            m.width ? `${m.width}×${m.height}` : null,
            m.usage_count ? `used ${m.usage_count}×` : 'unused',
          ].filter(Boolean).join(' · '),
        }),
      ]),
      m.alt_text ? null : h('span.media-flag', { text: 'no alt text' }),
    ]);
  }

  async function openFile(ctx, mediaId) {
    const { media: m } = await api.get(`/api/media/${mediaId}`);
    const altInput = textInput('alt_text', m.alt_text, { maxlength: 300 });
    const titleInput = textInput('title', m.title, { maxlength: 200 });
    const captionInput = textarea('caption', m.caption, { rows: 2, maxlength: 600 });
    const tagsInput = textInput('tags', (m.tags || []).join(', '));

    const usage = m.usage || [];
    const variants = m.variants || [];

    formDrawer({
      title: m.original_filename,
      subtitle: `${m.mime_type} · ${bytes(m.byte_size)}`
        + (m.width ? ` · ${m.width}×${m.height}` : ''),
      fields: [
        { name: 'alt_text', label: 'Alt text', control: altInput,
          help: 'Describes the image for screen readers and search engines. '
            + 'Leave blank only for purely decorative images.' },
        { name: 'title', label: 'Title', control: titleInput },
        { name: 'caption', label: 'Caption', control: captionInput },
        { name: 'tags', label: 'Tags', control: tagsInput, help: 'Comma separated.' },
      ],
      onSave: async (values) => {
        await api.patch(`/api/media/${m.id}`, {
          alt_text: values.alt_text || null,
          title: values.title || null,
          caption: values.caption || null,
          tags: values.tags ? values.tags.split(',').map((s) => s.trim()).filter(Boolean) : [],
        });
        toast('Saved.');
        ctx.reload();
      },
      extra: h('div.stack', {}, [
        m.isImage ? h('img.media-preview', { src: m.url, alt: m.alt_text || '' }) : null,
        h('hr'),
        h('h3', { text: 'URLs' }),
        copyRow('Original', m.url),
        ...Object.entries(m.srcset || {}).map(([mime, value]) =>
          copyRow(`srcset (${mime})`, value)),
        variants.length
          ? h('p.muted', { text: `${variants.length} derivative(s) generated.` })
          : h('p.muted', { text: 'No derivatives — this is not a raster image.' }),
        h('hr'),
        h('h3', { text: 'Used by' }),
        usage.length
          ? h('ul.plain', {}, usage.map((u) =>
            h('li', { text: `${u.label} (${u.object_type.replace('_', ' ')})` })))
          : h('p.muted', { text: 'Not referenced anywhere. Safe to delete.' }),
        h('hr'),
        h('div.row-actions', {}, [
          m.deleted_at
            ? actionButton('Restore', async () => {
              await api.post(`/api/media/${m.id}/restore`, {});
              toast('Restored.');
              ctx.reload();
            })
            : confirmButton('Move to trash', async () => {
              try {
                await api.del(`/api/media/${m.id}`);
              } catch (err) {
                if (err.status !== 409) throw err;
                // The API refuses while it is in use; say so and offer force.
                toast(err.message, 'error');
                throw new Error('Still in use — use “Delete anyway”.');
              }
              toast('Moved to trash.');
              ctx.reload();
            }),
          usage.length && !m.deleted_at
            ? confirmButton('Delete anyway', async () => {
              await api.del(`/api/media/${m.id}?force=true`);
              toast('Moved to trash. References to it are now broken.');
              ctx.reload();
            })
            : null,
          m.deleted_at
            ? confirmButton('Delete permanently', async () => {
              await api.del(`/api/media/${m.id}/purge?confirm=true`);
              toast('File and every derivative deleted.');
              ctx.reload();
            })
            : null,
        ]),
      ]),
    });
  }

  function copyRow(label, value) {
    const input = h('input', { type: 'text', value, readonly: 'readonly' });
    return h('div.field-inline', {}, [
      h('span.muted', { text: label }),
      input,
      h('button.btn.btn-sm', {
        type: 'button', text: 'Copy',
        onclick: () => {
          input.select();
          navigator.clipboard?.writeText(value).catch(() => {});
          toast('Copied.');
        },
      }),
    ]);
  }

  // ------------------------------------------------------------- upload
  function uploadButton(ctx) {
    const input = h('input', {
      type: 'file', multiple: 'multiple', class: 'hidden',
      onchange: (e) => upload(ctx, [...e.target.files]),
    });
    return h('div.row-actions', {}, [
      input,
      h('button.btn.btn-primary', {
        type: 'button', text: 'Upload files', onclick: () => input.click(),
      }),
    ]);
  }

  function wireDropzone(ctx, node) {
    ['dragenter', 'dragover'].forEach((event) =>
      node.addEventListener(event, (e) => {
        e.preventDefault();
        node.dataset.active = '1';
      }));
    ['dragleave', 'drop'].forEach((event) =>
      node.addEventListener(event, () => delete node.dataset.active));
    node.addEventListener('drop', (e) => {
      e.preventDefault();
      upload(ctx, [...(e.dataTransfer?.files || [])]);
    });
  }

  async function upload(ctx, files) {
    if (!files.length) return;
    let done = 0;
    let failed = 0;

    for (const file of files.slice(0, 25)) {
      const body = new FormData();
      body.append('file', file);
      if (ctx.params.folder_id && ctx.params.folder_id !== '0') {
        body.append('folder_id', ctx.params.folder_id);
      }
      try {
        // Deliberately not api.post: that JSON-encodes the body.
        const res = await fetch('/api/media', {
          method: 'POST',
          body,
          credentials: 'same-origin',
          headers: { 'x-csrf-token': api.csrf() },
        });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(payload.error || `Upload failed (${res.status})`);
        done += 1;
        if (payload.duplicate) toast(`${file.name} was already in the library.`);
      } catch (err) {
        failed += 1;
        toast(`${file.name}: ${err.message}`, 'error');
      }
    }

    if (done) toast(`${done} file(s) uploaded.`);
    if (done || !failed) ctx.reload();
  }

  // ------------------------------------------------------------ folders
  function manageFolders(ctx, folders) {
    const nameInput = textInput('name', '');
    openDrawer({
      title: 'Folders',
      subtitle: 'Deleting a folder unfiles its media rather than deleting it.',
      body: [
        table([
          { label: 'Folder', cell: (f) => f.name },
          { label: 'Files', class: 'cell-mono', cell: (f) => f.file_count },
          {
            label: '',
            cell: (f) => confirmButton('Delete', async () => {
              const result = await api.del(`/api/media/folders/${f.id}`);
              toast(`Folder deleted. ${result.filesUnfiled} file(s) unfiled.`);
              ctx.reload();
            }, { small: true }),
          },
        ], folders, { empty: 'No folders yet.' }),
        h('div.panel-body', {}, [
          h('label.field', {}, [h('span', { text: 'New folder' }), nameInput]),
          actionButton('Create folder', async () => {
            if (!nameInput.value.trim()) throw new Error('Give the folder a name.');
            await api.post('/api/media/folders', { name: nameInput.value.trim() });
            toast('Folder created.');
            ctx.reload();
          }, { primary: true }),
        ]),
      ],
    });
  }

  window.mediaViews = { media };
}());
