using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Tasks;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.MediaPreviewBridge.Markers;

/// <summary>
/// Deletes stored markers of items Jellyfin no longer has. <see cref="MarkerCleanupService"/> handles single removals,
/// but Jellyfin 10.11 reports only the folder when a whole show or season folder is removed, so its episodes' store
/// files would stay behind.
/// </summary>
public sealed class MarkerStoreSweepTask : IScheduledTask
{
    // Jellyfin fires the startup trigger before its own startup has completed, so a run waits (bounded) for that and for
    // a running library scan to finish.
    private static readonly TimeSpan MaxWait = TimeSpan.FromMinutes(10);
    private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(2);

    private readonly ILibraryManager _libraryManager;
    private readonly IServerApplicationHost _appHost;
    private readonly ILogger<MarkerStoreSweepTask> _logger;

    /// <summary>Initializes a new instance of the <see cref="MarkerStoreSweepTask"/> class.</summary>
    /// <param name="libraryManager">Library manager, to look items up.</param>
    /// <param name="appHost">Server host, to know whether startup has finished.</param>
    /// <param name="logger">Logger.</param>
    public MarkerStoreSweepTask(ILibraryManager libraryManager, IServerApplicationHost appHost, ILogger<MarkerStoreSweepTask> logger)
    {
        _libraryManager = libraryManager;
        _appHost = appHost;
        _logger = logger;
    }

    /// <inheritdoc />
    public string Name => "Media Preview Bridge: clean up Intro & Credits markers";

    /// <inheritdoc />
    public string Key => "MediaPreviewBridgeMarkerStoreSweep";

    /// <inheritdoc />
    public string Description => "Deletes the Intro & Credits markers Media Preview Bridge stored for items that are no longer in the library.";

    /// <inheritdoc />
    public string Category => "Maintenance";

    /// <inheritdoc />
    public IEnumerable<TaskTriggerInfo> GetDefaultTriggers()
    {
        // Once after every start (plugin installs and updates restart Jellyfin), then daily.
        yield return new TaskTriggerInfo { Type = TaskTriggerInfoType.StartupTrigger };
        yield return new TaskTriggerInfo { Type = TaskTriggerInfoType.DailyTrigger, TimeOfDayTicks = TimeSpan.FromHours(3).Ticks };
    }

    /// <inheritdoc />
    public async Task ExecuteAsync(IProgress<double> progress, CancellationToken cancellationToken)
    {
        var skipReason = SkipReason();
        for (var waited = TimeSpan.Zero; skipReason is not null && waited < MaxWait; waited += PollInterval)
        {
            await Task.Delay(PollInterval, cancellationToken).ConfigureAwait(false);
            skipReason = SkipReason();
        }

        if (skipReason is not null)
        {
            _logger.LogInformation("Media Preview Bridge: marker cleanup skipped: {Reason}", skipReason);
            progress.Report(100);
            return;
        }

        var ids = MarkerStore.StoredItemIds();
        var removed = 0;
        for (var i = 0; i < ids.Count; i++)
        {
            cancellationToken.ThrowIfCancellationRequested();
            try
            {
                if (MarkerStore.DeleteIfGone(ids[i], id => _libraryManager.GetItemById(id) is not null))
                {
                    removed++;
                }
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
                // Type name only, no paths; the next run tries again.
                _logger.LogWarning("Media Preview Bridge: couldn't remove the stored markers of {ItemId} ({Error})", ids[i], ex.GetType().Name);
            }

            progress.Report(100.0 * (i + 1) / ids.Count);
        }

        _logger.LogInformation(
            "Media Preview Bridge: marker cleanup checked {Checked} stored item(s), removed {Removed}",
            ids.Count,
            removed);
        progress.Report(100);
    }

    /// <summary>
    /// Why deleting now could remove markers of items that still exist; null when it is safe. A lookup that fails while
    /// Jellyfin starts or scans would read every item as gone.
    /// </summary>
    private string? SkipReason()
    {
        if (!_appHost.CoreStartupHasCompleted)
        {
            return "Jellyfin is still starting";
        }

        if (_libraryManager.IsScanRunning)
        {
            return "a library scan is running";
        }

        var root = _libraryManager.RootFolder;
        if (root is null || _libraryManager.GetItemById(root.Id) is null)
        {
            return "library lookups aren't available yet";
        }

        return null;
    }
}
