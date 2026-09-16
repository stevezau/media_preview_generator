using MediaBrowser.Controller;
using MediaBrowser.Controller.MediaSegments;
using MediaBrowser.Controller.Plugins;
using Microsoft.Extensions.DependencyInjection;
namespace MarkersLab;
public class Registrator : IPluginServiceRegistrator
{
    public void RegisterServices(IServiceCollection s, IServerApplicationHost h) => s.AddSingleton<IMediaSegmentProvider, Provider>();
}
