using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.MediaPreviewBridge.Markers;

/// <summary>
/// Deletes an item's stored markers when Jellyfin removes the item. Jellyfin 10.11 has no provider cleanup hook, and
/// item ids come from the path, so without this a file deleted and added back under the same name would get the old
/// markers again, and store files for removed items would pile up.
/// </summary>
public sealed class MarkerCleanupService : IHostedService
{
    private readonly ILibraryManager _libraryManager;
    private readonly ILogger<MarkerCleanupService> _logger;

    /// <summary>Initializes a new instance of the <see cref="MarkerCleanupService"/> class.</summary>
    /// <param name="libraryManager">Library manager whose item removals are followed.</param>
    /// <param name="logger">Logger.</param>
    public MarkerCleanupService(ILibraryManager libraryManager, ILogger<MarkerCleanupService> logger)
    {
        _libraryManager = libraryManager;
        _logger = logger;
    }

    /// <inheritdoc />
    public Task StartAsync(CancellationToken cancellationToken)
    {
        _libraryManager.ItemRemoved += OnItemRemoved;
        return Task.CompletedTask;
    }

    /// <inheritdoc />
    public Task StopAsync(CancellationToken cancellationToken)
    {
        _libraryManager.ItemRemoved -= OnItemRemoved;
        return Task.CompletedTask;
    }

    private void OnItemRemoved(object? sender, ItemChangeEventArgs e)
    {
        if (e.Item is not Video video)
        {
            return;
        }

        try
        {
            if (MarkerStore.Delete(video.Id))
            {
                _logger.LogInformation("Media Preview Bridge: removed the stored markers of deleted item {ItemId}", video.Id);
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Never let a store problem interrupt Jellyfin's own removal; the type name only, no paths.
            _logger.LogWarning("Media Preview Bridge: couldn't remove the stored markers of deleted item {ItemId} ({Error})", video.Id, ex.GetType().Name);
        }
    }
}
