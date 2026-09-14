using System;
using System.Collections.Generic;
using System.Globalization;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Net;
using MediaBrowser.Controller.Persistence;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.Logging;
using MediaBrowser.Model.Serialization;
using MediaBrowser.Model.Services;
using MediaPreviewBridge.Emby.Markers;

namespace MediaPreviewBridge.Emby.Api
{
    [Route("/MediaPreviewBridge/Ping", "GET", Summary = "Media Preview Bridge presence and features")]
    [Unauthenticated]
    public class PingRequest : IReturn<PingResponse>
    {
    }

    public class PingResponse
    {
        public bool Ok { get; set; }

        public string Version { get; set; }

        public string[] Features { get; set; }
    }

    [Route("/MediaPreviewBridge/Markers/{Id}", "GET", Summary = "Stored markers for an item")]
    [Authenticated(Roles = "admin")]
    public class GetMarkersRequest : IReturn<MarkersResponse>
    {
        public string Id { get; set; }
    }

    [Route("/MediaPreviewBridge/Markers/{Id}", "POST", Summary = "Replace an item's markers")]
    [Authenticated(Roles = "admin")]
    public class SetMarkersRequest : IReturn<MarkersResponse>
    {
        public string Id { get; set; }

        public long? IntroStartTicks { get; set; }

        public long? IntroEndTicks { get; set; }

        public long? CreditsStartTicks { get; set; }

        public long? FileSize { get; set; }

        /// <summary>
        /// Gets or sets a value indicating whether our intro or credits replace rows of that type the plugin didn't write
        /// (Emby's own detection, other plugins). False: those rows stay and the type gets none of ours.
        /// </summary>
        public bool ReplaceOwn { get; set; }
    }

    [Route("/MediaPreviewBridge/Markers/{Id}", "DELETE", Summary = "Remove an item's markers")]
    [Authenticated(Roles = "admin")]
    public class DeleteMarkersRequest : IReturn<MarkersResponse>
    {
        public string Id { get; set; }
    }

    public class MarkersResponse
    {
        public string Id { get; set; }

        public bool Found { get; set; }

        public string Error { get; set; }

        public long? IntroStartTicks { get; set; }

        public long? IntroEndTicks { get; set; }

        public long? CreditsStartTicks { get; set; }

        public long? FileSize { get; set; }

        public bool Stale { get; set; }

        public int Stored { get; set; }
    }

    public class BridgeService : IService, IRequiresRequest
    {
        private readonly ILibraryManager _libraryManager;
        private readonly IItemRepository _itemRepository;
        private readonly MarkerStore _store;
        private readonly ILogger _log;

        public BridgeService(ILibraryManager libraryManager, IItemRepository itemRepository, IJsonSerializer json, ILogManager logManager)
        {
            _libraryManager = libraryManager;
            _itemRepository = itemRepository;
            _log = logManager.GetLogger("MediaPreviewBridge");
            _store = new MarkerStore(json, _log);
        }

        /// <summary>Gets or sets the current request (set by Emby), used to answer failures with a 500 status.</summary>
        public IRequest Request { get; set; }

        public object Get(PingRequest request) =>
            new PingResponse { Ok = true, Version = Plugin.Instance.Version.ToString(), Features = new[] { "markers" } };

        public object Get(GetMarkersRequest request)
        {
            try
            {
                var item = Find(request.Id);
                if (item == null) return NotFound(request.Id);
                return Describe(item, _store.Load(item.InternalId), MarkerChapters.CurrentFileSize(item), 0);
            }
            catch (Exception ex)
            {
                return Failed(request.Id, "couldn't read the item's markers", ex);
            }
        }

        public object Post(SetMarkersRequest request)
        {
            BaseItem item;
            try
            {
                item = Find(request.Id);
            }
            catch (Exception ex)
            {
                return Failed(request.Id, "couldn't look the item up", ex);
            }

            if (item == null) return NotFound(request.Id);
            if (!(item is Video)) return new MarkersResponse { Id = request.Id, Found = true, Error = "item is not a video" };
            var markers = new StoredMarkers
            {
                IntroStartTicks = request.IntroStartTicks,
                IntroEndTicks = request.IntroEndTicks,
                CreditsStartTicks = request.CreditsStartTicks,
                FileSize = request.FileSize,
                Path = item.Path,
            };
            var problem = StoredMarkers.Problem(markers);
            if (problem != null) return new MarkersResponse { Id = request.Id, Found = true, Error = problem };

            // Outside the lock: a stalled media mount must not hold up every other marker call.
            var currentSize = MarkerChapters.CurrentFileSize(item);
            // A stale set is stored (the app sees Stale=true and says why) but never shown on a different file.
            var stale = StoredMarkers.IsStale(markers, currentSize, item.Path);
            int written;
            lock (MarkerStore.Gate)
            {
                StoredMarkers previous;
                List<ChapterInfo> existing;
                try
                {
                    previous = _store.Load(item.InternalId);
                    existing = _itemRepository.GetChapters(item);
                }
                catch (Exception ex)
                {
                    return Failed(request.Id, "couldn't read the item's markers", ex);
                }

                var rows = MarkerChapters.Apply(existing, previous, stale ? null : markers, request.ReplaceOwn, out written);
                var failure = WriteStoreThenChapters(item, existing, rows, previous, () => _store.Save(item.InternalId, markers));
                if (failure != null) return failure;
            }

            _log.Info("Media Preview Bridge: stored markers for item {0}", item.InternalId);
            return Describe(item, markers, currentSize, written);
        }

        public object Delete(DeleteMarkersRequest request)
        {
            BaseItem item;
            try
            {
                item = Find(request.Id);
            }
            catch (Exception ex)
            {
                return Failed(request.Id, "couldn't look the item up", ex);
            }

            if (item == null) return NotFound(request.Id);
            lock (MarkerStore.Gate)
            {
                StoredMarkers stored;
                List<ChapterInfo> existing;
                try
                {
                    stored = _store.Load(item.InternalId);
                    existing = _itemRepository.GetChapters(item);
                }
                catch (Exception ex)
                {
                    return Failed(request.Id, "couldn't read the item's markers", ex);
                }

                var rows = MarkerChapters.Apply(existing, stored, null, false, out _);
                var failure = WriteStoreThenChapters(item, existing, rows, stored, () => _store.Delete(item.InternalId));
                if (failure != null) return failure;
            }

            return Describe(item, null, null, 0);
        }

        /// <summary>
        /// Store first, then the chapter rows. If the rows can't be written, the store gets back what it held before,
        /// so it keeps describing the rows on the item (the healer and DELETE rely on that). Returns the 500 answer, or
        /// null when both writes went through. Callers hold <see cref="MarkerStore.Gate"/>.
        /// </summary>
        private MarkersResponse WriteStoreThenChapters(BaseItem item, List<ChapterInfo> existing, List<ChapterInfo> rows, StoredMarkers previous, Action writeStore)
        {
            try
            {
                writeStore();
            }
            catch (Exception ex)
            {
                return Failed(Id(item), "couldn't update the marker store", ex);
            }

            if (MarkerChapters.SameRows(existing, rows)) return null;
            try
            {
                _itemRepository.SaveChapters(item.InternalId, rows);
                return null;
            }
            catch (Exception ex)
            {
                RestoreStore(item.InternalId, previous);
                return Failed(Id(item), "couldn't write the item's chapters", ex);
            }
        }

        private void RestoreStore(long internalId, StoredMarkers previous)
        {
            try
            {
                if (previous != null) _store.Save(internalId, previous);
                else _store.Delete(internalId);
            }
            catch (Exception ex)
            {
                _log.ErrorException("Media Preview Bridge: restoring the marker store failed for item " + internalId.ToString(CultureInfo.InvariantCulture), ex);
            }
        }

        /// <summary>The 500 answer: same JSON shape, so a caller never has to parse Emby's error page.</summary>
        private MarkersResponse Failed(string id, string what, Exception ex)
        {
            _log.ErrorException("Media Preview Bridge: " + what + " for item " + id, ex);
            if (Request?.Response != null) Request.Response.StatusCode = 500;
            return new MarkersResponse { Id = id, Found = true, Error = what + " (" + ex.GetType().Name + ")" };
        }

        private BaseItem Find(string id) =>
            long.TryParse(id, NumberStyles.None, CultureInfo.InvariantCulture, out var internalId) ? _libraryManager.GetItemById(internalId) : null;

        private static MarkersResponse NotFound(string id) => new MarkersResponse { Id = id, Found = false, Error = "item not found" };

        private static string Id(BaseItem item) => item.InternalId.ToString(CultureInfo.InvariantCulture);

        private static MarkersResponse Describe(BaseItem item, StoredMarkers markers, long? currentSize, int stored) => new MarkersResponse
        {
            Id = Id(item),
            Found = true,
            IntroStartTicks = markers?.IntroStartTicks,
            IntroEndTicks = markers?.IntroEndTicks,
            CreditsStartTicks = markers?.CreditsStartTicks,
            FileSize = markers?.FileSize,
            Stale = StoredMarkers.IsStale(markers, currentSize, item.Path),
            Stored = stored,
        };
    }
}
