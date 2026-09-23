using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using MediaBrowser.Model.MediaSegments;
namespace MarkersLab;
public record StoredSegment(Jellyfin.Database.Implementations.Enums.MediaSegmentType Type, long StartTicks, long EndTicks);
public static class Store
{
    static string Dir => Path.Combine(Plugin.Instance!.DataFolderPath, "markers");
    static string FileFor(Guid id) => Path.Combine(Dir, id.ToString("N") + ".json");
    public static void Save(Guid id, List<StoredSegment> segs) { Directory.CreateDirectory(Dir); File.WriteAllText(FileFor(id), JsonSerializer.Serialize(segs)); }
    public static List<StoredSegment> Load(Guid id) => File.Exists(FileFor(id)) ? JsonSerializer.Deserialize<List<StoredSegment>>(File.ReadAllText(FileFor(id)))! : new();
    public static void Delete(Guid id) { if (File.Exists(FileFor(id))) File.Delete(FileFor(id)); }
}
