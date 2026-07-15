#include <GL/gl.h>
#include <GL/glx.h>
#include <X11/Xlib.h>
#include <dlfcn.h>
#include <stdbool.h>
#include <stdio.h>

#include "renderdoc_app.h"

int main(void) {
    Display *display = XOpenDisplay(NULL);
    int attributes[] = {GLX_RGBA, GLX_DOUBLEBUFFER, GLX_DEPTH_SIZE, 24, None};
    XVisualInfo *visual = display ? glXChooseVisual(display, DefaultScreen(display), attributes) : NULL;
    if (!display || !visual) return 2;

    Window root = RootWindow(display, visual->screen);
    Colormap color_map = XCreateColormap(display, root, visual->visual, AllocNone);
    XSetWindowAttributes window_attributes = {0};
    window_attributes.colormap = color_map;
    window_attributes.event_mask = ExposureMask;
    Window window = XCreateWindow(display, root, 0, 0, 320, 200, 0, visual->depth, InputOutput,
                                  visual->visual, CWColormap | CWEventMask, &window_attributes);
    GLXContext context = glXCreateContext(display, visual, NULL, True);
    XMapWindow(display, window);
    glXMakeCurrent(display, window, context);

    pRENDERDOC_GetAPI get_api = (pRENDERDOC_GetAPI)dlsym(RTLD_DEFAULT, "RENDERDOC_GetAPI");
    RENDERDOC_API_1_1_2 *api = NULL;
    if (!get_api || !get_api(eRENDERDOC_API_Version_1_1_2, (void **)&api)) return 3;
    api->StartFrameCapture(NULL, NULL);
    glViewport(0, 0, 320, 200);
    glClearColor(0.1f, 0.4f, 0.8f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    glXSwapBuffers(display, window);
    api->EndFrameCapture(NULL, NULL);

    glXMakeCurrent(display, None, NULL);
    glXDestroyContext(display, context);
    XDestroyWindow(display, window);
    XCloseDisplay(display);
    puts("captured");
    return 0;
}
