# coding: utf-8

def get_num_gen(gen):
    return sum(1 for x in gen)


def is_leaf(model):
    return get_num_gen(model.children()) == 0


def get_layer_info(layer):
    layer_str = str(layer)
    type_name = layer_str[:layer_str.find('(')].strip()
    return type_name


LINE_UP = '\033[1A'
LINE_CLEAR = '\x1b[2K'

def KSE(model, G=None, T=None, verbose=False):
    for child in model.children():
        if is_leaf(child):
            if get_layer_info(child) in ["Conv2d_KSE"]:
                child.KSE(G=G, T=T)
                # if verbose:
                print(LINE_UP, end=LINE_CLEAR)
                print(child, "KSE finish!")
        else:
            KSE(child, G=G, T=T)


def forward_init(model):
    n_remaining, n_total = 0, 0
    for child in model.children():
        if is_leaf(child):
            if get_layer_info(child) in ["Conv2d_KSE"]:
                a, b = child.forward_init()
                n_remaining += a
                n_total += b
                # print(child, "forward_init finish!")
        else:
            a, b = forward_init(child)
            n_remaining += a
            n_total += b
    return n_remaining, n_total


def create_arch(model, G=None, T=None):
    for child in model.children():
        if is_leaf(child):
            if get_layer_info(child) in ["Conv2d_KSE"]:
                child.create_arch(G=G, T=T)
                print(child, "create arch finish!")
        else:
            create_arch(child, G=G, T=T)


def load(model):
    for child in model.children():
        if is_leaf(child):
            if get_layer_info(child) in ["Conv2d_KSE"]:
                child.load()
                print(child, "load finish!")
        else:
            load(child)


def save(model):
    for child in model.children():
        if is_leaf(child):
            if get_layer_info(child) in ["Conv2d_KSE"]:
                child.save()
                # print(child, "save finish!")
        else:
            save(child)