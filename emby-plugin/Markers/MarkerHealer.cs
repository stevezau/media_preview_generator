using System;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Persistence;
using MediaBrowser.Controller.Plugins;
using MediaBrowser.Model.Logging;
using MediaBrowser.Model.Serialization;

namespace MediaPreviewBridge.Emby.Markers
{
    /// <summary>
    /// Emby's "Replace all metadata" and "Search for missing metadata" refreshes delete every marker row. The item is
    /// updated in the same refresh, so markers are written back right there for each type that has no rows and while
    /// the file is still the one they were detected on. When the item's file was replaced (other size or path), our
    /// rows are removed instead. A write Emby stopped before its rows were saved is finished at the item's next update
    /// while the file is still the one it was sent for; for a replaced file the next POST or DELETE clears it.
    /// Also forgets removed items.
    /// </summary>
    public class MarkerHealer : IServerEntryPoint
    {
        private readonly ILibraryManager _libraryManager;
        private readonly IItemRepository _itemRepository;
        private readonly MarkerStore _store;
        private readonly ILogger _log;

        public MarkerHealer(ILibraryManager libraryManager, IItemRepository itemRepository, IJsonSerializer json, ILogManager logManager)
        {
            _libraryManager = libraryManager;
            _itemRepository = itemRepository;
            _log = logManager.GetLogger("MediaPreviewBridge");
            _store = new MarkerStore(json, _log);
        }

        public void Run()
        {
            _log.Info("Media Preview Bridge: marker store {0}", Plugin.Instance.MarkerStoreDir);
            try
            {
                var removed = _store.DeleteTempFiles();
                if (removed > 0) _log.Info("Media Preview Bridge: removed {0} unfinished marker file(s)", removed);
            }
            catch (Exception ex)
            {
                _log.ErrorException("Media Preview Bridge: removing unfinished marker files failed", ex);
            }

            _libraryManager.ItemUpdated += OnItemUpdated;
            _libraryManager.ItemRemoved += OnItemRemoved;
        }

        public void Dispose()
        {
            _libraryManager.ItemUpdated -= OnItemUpdated;
            _libraryManager.ItemRemoved -= OnItemRemoved;
        }

        private void OnItemUpdated(object sender, ItemChangeEventArgs e)
        {
            try
            {
                if (!(e.Item is Video item) || !_store.Exists(item.InternalId)) return;
                // Outside the lock: a stalled media mount must not hold up every other marker call.
                var currentSize = MarkerChapters.CurrentFileSize(item);
                lock (MarkerStore.Gate)
                {
                    var stored = _store.Load(item.InternalId);
                    if (stored == null) return;
                    var existing = _itemRepository.GetChapters(item);
                    if (StoredMarkers.IsStale(stored, currentSize, item.Path))
                    {
                        var withoutOurs = MarkerChapters.Apply(existing, stored, null, false, out _);
                        if (MarkerChapters.SameRows(existing, withoutOurs)) return;
                        _itemRepository.SaveChapters(item.InternalId, withoutOurs);
                        _log.Info("Media Preview Bridge: removed markers of a replaced file for item {0}", item.InternalId);
                        return;
                    }

                    // Rows of the set a stopped write was replacing give way to the stored ones.
                    var rows = MarkerChapters.Apply(existing, stored.Replacing, stored, false, out _);
                    if (!MarkerChapters.SameRows(existing, rows))
                    {
                        _itemRepository.SaveChapters(item.InternalId, rows);
                        _log.Info("Media Preview Bridge: markers written back for item {0} ({1})", item.InternalId, e.UpdateReason);
                    }

                    if (stored.Replacing != null)
                    {
                        _store.Save(item.InternalId, stored, null);
                        _log.Info("Media Preview Bridge: finished an interrupted marker write for item {0}", item.InternalId);
                    }
                }
            }
            catch (Exception ex)
            {
                // An event handler that throws breaks Emby's own refresh of the item.
                _log.ErrorException("Media Preview Bridge: writing markers back failed for an item", ex);
            }
        }

        private void OnItemRemoved(object sender, ItemChangeEventArgs e)
        {
            try
            {
                if (e.Item != null && _store.Delete(e.Item.InternalId))
                {
                    _log.Info("Media Preview Bridge: forgot markers of removed item {0}", e.Item.InternalId);
                }
            }
            catch (Exception ex)
            {
                _log.ErrorException("Media Preview Bridge: removing stored markers failed", ex);
            }
        }
    }
}
