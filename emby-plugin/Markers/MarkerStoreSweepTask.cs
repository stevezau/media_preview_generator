using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Logging;
using MediaBrowser.Model.Serialization;
using MediaBrowser.Model.Tasks;

namespace MediaPreviewBridge.Emby.Markers
{
    /// <summary>
    /// Deletes stored markers of items Emby no longer has. <see cref="MarkerHealer"/> handles single removals, but when
    /// a whole show folder is removed Emby reports only the show, so its episodes' store files would stay behind.
    /// </summary>
    public class MarkerStoreSweepTask : IScheduledTask
    {
        // The startup trigger can fire before Emby's own startup has finished, so a run waits (bounded) for that and
        // for a running library scan.
        private static readonly TimeSpan MaxWait = TimeSpan.FromMinutes(10);
        private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(2);

        private readonly ILibraryManager _libraryManager;
        private readonly IServerApplicationHost _appHost;
        private readonly MarkerStore _store;
        private readonly ILogger _log;

        public MarkerStoreSweepTask(ILibraryManager libraryManager, IServerApplicationHost appHost, IJsonSerializer json, ILogManager logManager)
        {
            _libraryManager = libraryManager;
            _appHost = appHost;
            _log = logManager.GetLogger("MediaPreviewBridge");
            _store = new MarkerStore(json, _log);
        }

        public string Name => "Media Preview Bridge: clean up Intro & Credits markers";

        public string Key => "MediaPreviewBridgeMarkerStoreSweep";

        public string Description => "Deletes the Intro & Credits markers Media Preview Bridge stored for items that are no longer in the library.";

        public string Category => "Maintenance";

        public IEnumerable<TaskTriggerInfo> GetDefaultTriggers()
        {
            // Once after every start (installing or updating the plugin restarts Emby), then daily.
            yield return new TaskTriggerInfo { Type = TaskTriggerInfo.TriggerStartup };
            yield return new TaskTriggerInfo { Type = TaskTriggerInfo.TriggerDaily, TimeOfDayTicks = TimeSpan.FromHours(3).Ticks };
        }

        public async Task Execute(CancellationToken cancellationToken, IProgress<double> progress)
        {
            var skipReason = SkipReason();
            for (var waited = TimeSpan.Zero; skipReason != null && waited < MaxWait; waited += PollInterval)
            {
                await Task.Delay(PollInterval, cancellationToken).ConfigureAwait(false);
                skipReason = SkipReason();
            }

            if (skipReason != null)
            {
                _log.Info("Media Preview Bridge: marker cleanup skipped: {0}", skipReason);
                progress.Report(100);
                return;
            }

            var ids = _store.StoredItemIds();
            var removed = 0;
            for (var i = 0; i < ids.Count; i++)
            {
                cancellationToken.ThrowIfCancellationRequested();
                try
                {
                    if (_store.DeleteIfGone(ids[i], id => _libraryManager.GetItemById(id) != null)) removed++;
                }
                catch (Exception ex)
                {
                    // Nothing is deleted when the lookup or the delete fails. Type name only, no paths; the next run
                    // tries again.
                    _log.Warn("Media Preview Bridge: couldn't clean up the stored markers of {0} ({1})", ids[i], ex.GetType().Name);
                }

                progress.Report(100.0 * (i + 1) / ids.Count);
            }

            _log.Info("Media Preview Bridge: marker cleanup checked {0} stored item(s), removed {1}", ids.Count, removed);
            progress.Report(100);
        }

        /// <summary>
        /// Why deleting now could remove markers of items that still exist; null when it is safe. A lookup that fails
        /// while Emby starts or scans would read every item as gone.
        /// </summary>
        private string SkipReason()
        {
            if (!_appHost.IsStartupComplete) return "Emby is still starting";
            if (_libraryManager.IsScanRunning) return "a library scan is running";
            if (_libraryManager.GetItemById(_libraryManager.RootFolderId) == null) return "library lookups aren't available yet";
            return null;
        }
    }
}
