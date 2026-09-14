using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using Jellyfin.Database.Implementations.Enums;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.MediaPreviewBridge.Markers;

/// <summary>One stored segment (ticks).</summary>
/// <param name="Type">Segment type.</param>
/// <param name="StartTicks">Start in ticks.</param>
/// <param name="EndTicks">End in ticks.</param>
public record StoredSegment(MediaSegmentType Type, long StartTicks, long EndTicks);

/// <summary>Everything stored for one item.</summary>
/// <param name="FileSize">Size in bytes of the media file the markers were detected on, when the caller sent it.</param>
/// <param name="Segments">Segments.</param>
public record StoredMarkers(long? FileSize, IReadOnlyList<StoredSegment> Segments)
{
    /// <summary>Gets the value used when nothing (usable) is stored.</summary>
    public static StoredMarkers Empty { get; } = new(null, Array.Empty<StoredSegment>());

    /// <summary>
    /// Whether the markers were detected on a different file than the one now on disk. Jellyfin 10.11 has no hook
    /// that tells providers a file was replaced, so the size is what keeps old markers off a new file.
    /// </summary>
    /// <param name="currentFileSize">Current on-disk size, or null when unknown.</param>
    /// <returns>True only when both sizes are known and differ.</returns>
    public bool IsStale(long? currentFileSize) =>
        FileSize is not null && currentFileSize is not null && FileSize != currentFileSize;
}

/// <summary>
/// Markers pushed by Media Preview Generator, one JSON file per item in the plugin data folder, so the provider can
/// hand the same segments back whenever Jellyfin re-runs segment providers (scans, refreshes, restarts).
/// </summary>
public static class MarkerStore
{
    /// <summary>Segment types the Bridge accepts and serves.</summary>
    public static readonly IReadOnlyList<MediaSegmentType> AllowedTypes = new[]
    {
        MediaSegmentType.Intro, MediaSegmentType.Outro, MediaSegmentType.Recap, MediaSegmentType.Preview,
    };

    private static readonly object Gate = new();

    private static string Dir => Path.Combine(Plugin.Instance!.DataFolderPath, "markers");

    /// <summary>
    /// Gets a value indicating whether markers were ever pushed to this server (the store folder exists). Until then the
    /// segment provider supports no item, so servers that only use trickplay do no per-item provider work.
    /// </summary>
    public static bool Exists => Directory.Exists(Dir);

    private static string FileFor(Guid itemId) => Path.Combine(Dir, itemId.ToString("N") + ".json");

    /// <summary>Whether a segment has an accepted type and sane ticks.</summary>
    /// <param name="segment">Segment to check.</param>
    /// <returns>True when valid.</returns>
    public static bool IsValid(StoredSegment? segment) =>
        segment is not null
        && AllowedTypes.Contains(segment.Type)
        && segment.StartTicks >= 0
        && segment.EndTicks > segment.StartTicks;

    /// <summary>Replace what is stored for an item.</summary>
    /// <param name="itemId">Item id.</param>
    /// <param name="markers">Markers to store.</param>
    public static void Save(Guid itemId, StoredMarkers markers)
    {
        lock (Gate)
        {
            Directory.CreateDirectory(Dir);
            // Write-then-rename so a crash mid-write never leaves a half file behind.
            var tmp = FileFor(itemId) + ".tmp";
            File.WriteAllText(tmp, JsonSerializer.Serialize(markers));
            File.Move(tmp, FileFor(itemId), overwrite: true);
        }
    }

    /// <summary>
    /// What is stored for an item. A missing file, or one that can't be read or doesn't hold valid markers, counts as
    /// nothing stored: Jellyfin calls the provider on every scan, so throwing here would break those runs for the item.
    /// </summary>
    /// <param name="itemId">Item id.</param>
    /// <param name="logger">Logger for the unreadable-file warning.</param>
    /// <returns>The stored markers, or <see cref="StoredMarkers.Empty"/>.</returns>
    public static StoredMarkers Load(Guid itemId, ILogger logger)
    {
        lock (Gate)
        {
            var path = FileFor(itemId);
            if (!File.Exists(path))
            {
                return StoredMarkers.Empty;
            }

            try
            {
                var markers = JsonSerializer.Deserialize<StoredMarkers>(File.ReadAllText(path));
                if (markers?.Segments is not null && markers.FileSize is null or > 0 && markers.Segments.All(IsValid))
                {
                    return markers;
                }

                logger.LogWarning("Media Preview Bridge: ignoring marker file for {ItemId}: not a valid marker set", itemId);
            }
            catch (Exception ex) when (ex is JsonException or IOException or UnauthorizedAccessException or NotSupportedException)
            {
                // Type name only: the message can quote the file's content.
                logger.LogWarning("Media Preview Bridge: ignoring unreadable marker file for {ItemId} ({Error})", itemId, ex.GetType().Name);
            }

            return StoredMarkers.Empty;
        }
    }

    /// <summary>Forget an item.</summary>
    /// <param name="itemId">Item id.</param>
    /// <returns>True when a store file was deleted.</returns>
    public static bool Delete(Guid itemId)
    {
        lock (Gate)
        {
            var path = FileFor(itemId);
            if (!File.Exists(path))
            {
                return false;
            }

            File.Delete(path);
            return true;
        }
    }
}
