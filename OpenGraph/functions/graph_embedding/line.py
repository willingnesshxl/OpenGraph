import sys
sys.path.append('../../../')
import OpenGraph as og
from OpenGraph.utils.alias import create_alias_table, alias_sample
from OpenGraph.utils.index_of_node import get_relation_of_index_and_node

import math
import random

import numpy as np
import tensorflow as tf
from tensorflow.python.keras import backend as K
from tensorflow.python.keras.layers import Embedding, Input, Lambda
from tensorflow.python.keras.models import Model


def line_loss(y_true, y_pred):
    y = K.sigmoid(y_true*y_pred)
    # Avoid Nan in the result of 'K.log'
    return -K.mean(K.log(tf.clip_by_value(y,1e-8,tf.reduce_max(y))))


def create_model(numNodes, embedding_size, order='second'):

    v_i = Input(shape=(1,))
    v_j = Input(shape=(1,))

    first_emb = Embedding(numNodes, embedding_size, name='first_emb')
    second_emb = Embedding(numNodes, embedding_size, name='second_emb')
    context_emb = Embedding(numNodes, embedding_size, name='context_emb')

    v_i_emb = first_emb(v_i)
    v_j_emb = first_emb(v_j)

    v_i_emb_second = second_emb(v_i)
    v_j_context_emb = context_emb(v_j)

    first = Lambda(lambda x: tf.reduce_sum(
        x[0]*x[1], axis=-1, keep_dims=False), name='first_order')([v_i_emb, v_j_emb])
    second = Lambda(lambda x: tf.reduce_sum(
        x[0]*x[1], axis=-1, keep_dims=False), name='second_order')([v_i_emb_second, v_j_context_emb])

    if order == 'first':
        output_list = [first]
    elif order == 'second':
        output_list = [second]
    else:
        output_list = [first, second]

    model = Model(inputs=[v_i, v_j], outputs=output_list)

    return model, {'first': first_emb, 'second': second_emb}


class LINE:
    def __init__(self, graph, embedding_size=8, negative_ratio=5, order='second',):
        """
        Parameters
        ----------
        graph : graph

        embedding_size : int
        
        negative_ratio : 

        order: 'first','second','all'
        """
        if order not in ['first', 'second', 'all']:
            raise ValueError('mode must be fisrt,second,or all')

        self.graph = graph
        self.idx2node, self.node2idx = get_relation_of_index_and_node(graph)
        self.use_alias = True

        self.rep_size = embedding_size
        self.order = order

        self._embeddings = {}
        self.negative_ratio = negative_ratio
        self.order = order

        self.node_size = graph.number_of_nodes()
        self.edge_size = graph.number_of_edges()
        self.samples_per_epoch = self.edge_size*(1+negative_ratio)

        self._gen_sampling_table()
        self.reset_model()

    def reset_training_config(self, batch_size, times):
        self.batch_size = batch_size
        self.steps_per_epoch = (
            (self.samples_per_epoch - 1) // self.batch_size + 1)*times

    def reset_model(self, opt='adam'):

        self.model, self.embedding_dict = create_model(
            self.node_size, self.rep_size, self.order)
        self.model.compile(opt, line_loss)
        self.batch_it = self.batch_iter(self.node2idx)

    def _gen_sampling_table(self):

        # create sampling table for vertex
        power = 0.75
        numNodes = self.node_size
        node_degree = np.zeros(numNodes)  # out degree
        node2idx = self.node2idx

        for edge in self.graph.edges:
            node_degree[node2idx[edge[0]]
                        ] += self.graph[edge[0]][edge[1]].get('weight', 1.0)

        total_sum = sum([math.pow(node_degree[i], power)
                         for i in range(numNodes)])
        norm_prob = [float(math.pow(node_degree[j], power)) /
                     total_sum for j in range(numNodes)]

        self.node_accept, self.node_alias = create_alias_table(norm_prob)

        # create sampling table for edge
        numEdges = self.graph.number_of_edges()
        total_sum = sum([self.graph[edge[0]][edge[1]].get('weight', 1.0)
                         for edge in self.graph.edges])
        norm_prob = [self.graph[edge[0]][edge[1]].get('weight', 1.0) *
                     numEdges / total_sum for edge in self.graph.edges]

        self.edge_accept, self.edge_alias = create_alias_table(norm_prob)

    def batch_iter(self, node2idx):

        edges = [(node2idx[x[0]], node2idx[x[1]]) for x in self.graph.edges]

        data_size = self.graph.number_of_edges()
        shuffle_indices = np.random.permutation(np.arange(data_size))
        # positive or negative mod
        mod = 0
        mod_size = 1 + self.negative_ratio
        h = []
        t = []
        sign = 0
        count = 0
        start_index = 0
        end_index = min(start_index + self.batch_size, data_size)
        while True:
            if mod == 0:

                h = []
                t = []
                for i in range(start_index, end_index):
                    if random.random() >= self.edge_accept[shuffle_indices[i]]:
                        shuffle_indices[i] = self.edge_alias[shuffle_indices[i]]
                    cur_h = edges[shuffle_indices[i]][0]
                    cur_t = edges[shuffle_indices[i]][1]
                    h.append(cur_h)
                    t.append(cur_t)
                sign = np.ones(len(h))
            else:
                sign = np.ones(len(h))*-1
                t = []
                for i in range(len(h)):

                    t.append(alias_sample(
                        self.node_accept, self.node_alias))

            if self.order == 'all':
                yield ([np.array(h), np.array(t)], [sign, sign])
            else:
                yield ([np.array(h), np.array(t)], [sign])
            mod += 1
            mod %= mod_size
            if mod == 0:
                start_index = end_index
                end_index = min(start_index + self.batch_size, data_size)

            if start_index >= data_size:
                count += 1
                mod = 0
                h = []
                shuffle_indices = np.random.permutation(np.arange(data_size))
                start_index = 0
                end_index = min(start_index + self.batch_size, data_size)

    def get_embeddings(self,):
        self._embeddings = {}
        if self.order == 'first':
            embeddings = self.embedding_dict['first'].get_weights()[0]
        elif self.order == 'second':
            embeddings = self.embedding_dict['second'].get_weights()[0]
        else:
            embeddings = np.hstack((self.embedding_dict['first'].get_weights()[
                                   0], self.embedding_dict['second'].get_weights()[0]))
        idx2node = self.idx2node
        for i, embedding in enumerate(embeddings):
            self._embeddings[idx2node[i]] = embedding

        return self._embeddings

    def train(self, batch_size=1024, epochs=1, initial_epoch=0, verbose=1, times=1):
        self.reset_training_config(batch_size, times)
        hist = self.model.fit_generator(self.batch_it, epochs=epochs, initial_epoch=initial_epoch, steps_per_epoch=self.steps_per_epoch,
                                        verbose=verbose)

        return hist


if __name__ == "__main__":

    # with open('./test_line/blog.txt', 'r') as fp:
    #     data = fp.readlines()
    # G = og.Graph()
    # edges = []
    # for edge in data:
    #     edge = edge.split()
    #     edges.append(edge)
    
    # print(len(edges))
    # import random
    # random.shuffle(edges)
    # for edge in edges[:1000]:
    #     try:
    #         G.add_edge(int(edge[0]), int(edge[1]))
    #     except:
    #         pass


    # 04/19
    from scipy.io import loadmat
    def sparse2graph(x):
        from collections import defaultdict
        from six import iteritems

        G = defaultdict(lambda: set())
        cx = x.tocoo()
        for i, j, v in zip(cx.row, cx.col, cx.data):
            G[i].add(j)
        return {str(k): [str(x) for x in v] for k, v in iteritems(G)}

    # mat = loadmat('./test_line/blogcatalog.mat')
    # A = mat['network']
    # gg = sparse2graph(A)

    # G = og.Graph()
    # for u in gg:
    #     for v in gg[u]:
    #         G.add_edge(u, v)

    G = og.Graph()
    G.add_edges_from_file(file='./test_line/release-youtube-links.txt')


    print('yes')
    model = LINE(G, embedding_size=16, order='all')
    model.train(batch_size=1024, epochs=1, verbose=2)
    embeddings = model.get_embeddings()

    with open('./test_line/youtube_line.embeddings', 'w') as fp:
        for node in embeddings:
            # print("{}: {}".format(node, embeddings[node]))
            fp.write(str(node) +' ')
            for pos_value in embeddings[node]:
                fp.write(str(pos_value) + ' ')
            fp.write('\n')

    