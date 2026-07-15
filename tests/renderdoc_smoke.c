#include <GL/gl.h>
#include <GL/glx.h>
#include <X11/Xlib.h>
#include <dlfcn.h>
#include <stdbool.h>
#include <stdio.h>
#include <unistd.h>

#include "renderdoc_app.h"

#define GLX_CONTEXT_MAJOR_VERSION_ARB 0x2091
#define GLX_CONTEXT_MINOR_VERSION_ARB 0x2092
#define GLX_CONTEXT_PROFILE_MASK_ARB 0x9126
#define GLX_CONTEXT_CORE_PROFILE_BIT_ARB 0x00000001

typedef GLXContext (*CreateContextAttribs)(Display *, GLXFBConfig, GLXContext, Bool, const int *);

int main(int argc, char **argv) {
    Display *display = XOpenDisplay(NULL);
    int count = 0;
    int attributes[] = {GLX_X_RENDERABLE, True, GLX_DRAWABLE_TYPE, GLX_WINDOW_BIT,
                        GLX_RENDER_TYPE, GLX_RGBA_BIT, GLX_DOUBLEBUFFER, True, None};
    GLXFBConfig *configs = display ? glXChooseFBConfig(display, DefaultScreen(display), attributes, &count) : NULL;
    XVisualInfo *visual = count > 0 ? glXGetVisualFromFBConfig(display, configs[0]) : NULL;
    CreateContextAttribs create_context =
        (CreateContextAttribs)glXGetProcAddressARB((const GLubyte *)"glXCreateContextAttribsARB");
    if (!display || !visual || !create_context) return 2;

    Window root = RootWindow(display, visual->screen);
    Colormap color_map = XCreateColormap(display, root, visual->visual, AllocNone);
    XSetWindowAttributes window_attributes = {0};
    window_attributes.colormap = color_map;
    window_attributes.event_mask = ExposureMask;
    Window window = XCreateWindow(display, root, 0, 0, 320, 200, 0, visual->depth, InputOutput,
                                  visual->visual, CWColormap | CWEventMask, &window_attributes);
    int context_attributes[] = {GLX_CONTEXT_MAJOR_VERSION_ARB, 3, GLX_CONTEXT_MINOR_VERSION_ARB, 2,
                                GLX_CONTEXT_PROFILE_MASK_ARB, GLX_CONTEXT_CORE_PROFILE_BIT_ARB, None};
    GLXContext context = create_context(display, configs[0], NULL, True, context_attributes);
    XFree(configs);
    if (!context) return 2;
    XMapWindow(display, window);
    glXMakeCurrent(display, window, context);

    pRENDERDOC_GetAPI get_api = (pRENDERDOC_GetAPI)dlsym(RTLD_DEFAULT, "RENDERDOC_GetAPI");
    RENDERDOC_API_1_1_2 *api = NULL;
    if (!get_api || !get_api(eRENDERDOC_API_Version_1_1_2, (void **)&api)) return 3;
    if (argc > 1) api->SetCaptureFilePathTemplate(argv[1]);

    glViewport(0, 0, 320, 200);
    glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    glXSwapBuffers(display, window);
    glFinish();

    api->StartFrameCapture(NULL, NULL);
    if (!api->IsFrameCapturing()) return 4;
    glViewport(0, 0, 320, 200);
    glClearColor(0.1f, 0.4f, 0.8f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    glXSwapBuffers(display, window);
    glFinish();
    if (!api->EndFrameCapture(NULL, NULL)) return 5;
    usleep(200000);

    glXMakeCurrent(display, None, NULL);
    glXDestroyContext(display, context);
    XDestroyWindow(display, window);
    XCloseDisplay(display);
    printf("captured=%u\n", api->GetNumCaptures());
    return 0;
}
