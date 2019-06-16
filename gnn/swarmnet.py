import os
import numpy as np
import tensorflow as tf
from tensorflow import keras

from .modules import *
from .utils import fc_matrix


class SwarmNet(keras.Model):
    def __init__(self, params):
        super().__init__(name='SwarmNet')

        # NOTE: For the moment assume Conv1D is always applied
        self.pred_steps = params['pred_steps']
        self.time_seg_len = params['time_seg_len']

        # Whether edge type used for model.
        self.edge_type = params['edge_type']
        self.skip_zero = 1 if self.edge_type > 1 and params.get('skip_zero', False) else 0

        if self.time_seg_len > 1:
            self.conv1d = Conv1D(params['cnn']['filters'], name='Conv1D')
        else:
            self.conv1d = keras.layers.Lambda(lambda x: x)

        if self.edge_type > 1:
            self.edge_encoders = [MLP(params['mlp']['hidden_units'], name=f'edge_encoder_{i}')
                                  for i in range(self.skip_zero, self.edge_type)]
        else:
            self.edge_encoder = MLP(params['mlp']['hidden_units'], name='edge_encoder')

        self.node_encoder = MLP(params['mlp']['hidden_units'], name='node_encoder')
        self.node_decoder = MLP(params['mlp']['hidden_units'], name='node_decoder')

        self.dense = keras.layers.Dense(params['ndims'], name='out_layer')

        edges = fc_matrix(params['nagents'])
        self.node_aggr = NodeAggregator(edges)
        self.edge_aggr = EdgeAggregator(edges)

    def build(self, input_shape):
        t = keras.layers.Input(input_shape[0][1:])
        if self.edge_type > 1:
            e = keras.layers.Input(input_shape[1][1:])

        self.call((t, e))
        self.built = True

    def _pred_next(self, time_segs, edge_types=None):
        # NOTE: For the moment, ignore edge_type.
        condensed_state = self.conv1d(time_segs)
        # condensed_state shape [batch, num_agents, 1, filters]

        # Form edges. Shape [batch, num_edges, 1, filters]
        edge_msg = self.node_aggr(condensed_state)

        if self.edge_type > 1:
            encoded_msg_by_type = []
            for i in range(self.skip_zero, self.edge_type):
                # mlp_encoder for each edge type.
                encoded_msg = self.edge_encoders[i](edge_msg)

                encoded_msg_by_type.append(encoded_msg)

            encoded_msg_by_type = tf.concat(encoded_msg_by_type, axis=2)
            # Shape [batch, num_edges, edge_types, hidden_units]

            edge_msg = tf.reduce_sum(tf.multiply(encoded_msg_by_type,
                                                 edge_types[:, :, self.skip_zero:, :]),
                                     axis=2,
                                     keepdims=True)
        else:
            # Shape [batch, num_edges, 1, hidden_units]
            edge_msg = self.edge_encoder(edge_msg)

        # Edge aggregation. Shape [batch, num_nodes, 1, filters]
        node_msg = self.edge_aggr(edge_msg)
        node_msg = self.node_encoder(node_msg)
        # The last state in each timeseries of the stack.
        prev_state = time_segs[:, :, -1:, :]
        # Skip connection
        node_state = tf.concat([prev_state, node_msg], axis=-1)
        node_state = self.node_decoder(node_state)

        # Predicted difference added to the prev state.
        next_state = self.dense(node_state) + prev_state
        return next_state

    def call(self, inputs):
        # time_segs shape [batch, time_seg_len, num_agents, ndims]
        # Transpose to [batch, num_agents, time_seg_len,ndims]
        time_segs = inputs[0]
        if self.edge_type > 1:
            edge_types = tf.expand_dims(inputs[1], axis=3)
            # Shape [None, n_edges, n_types, 1]
        else:
            edge_types = None

        extended_time_segs = tf.transpose(time_segs, [0, 2, 1, 3])

        for i in range(self.pred_steps):
            next_state = self._pred_next(extended_time_segs[:, :, i:, :], edge_types)
            extended_time_segs = tf.concat([extended_time_segs, next_state], axis=2)

        # Transpose back to [batch, time_seg_len+pred_steps, num_agetns, ndims]
        extended_time_segs = tf.transpose(extended_time_segs, [0, 2, 1, 3])

        # Return only the predicted part of extended_time_segs
        return extended_time_segs[:, self.time_seg_len:, :, :]


def build_model(params):
    model = SwarmNet(params)

    optimizer = keras.optimizers.Adam(lr=params['learning_rate'])
    loss = keras.losses.MeanSquaredError()

    model.compile(optimizer, loss=loss)

    if params['edge_type'] > 1:
        input_shape = [(None, params['time_seg_len'], params['nagents'], params['ndims']),
                       (None, params['nagents']*(params['nagents']-1), params['edge_type'])]
    else:
        input_shape = [(None, params['time_seg_len'], params['nagents'], params['ndims'])]

    model.build(input_shape)

    return model


def load_model(model, log_dir):
    checkpoint = os.path.join(log_dir, 'weights.h5')
    if os.path.exists(checkpoint):
        model.load_weights(checkpoint)


def save_model(model, log_dir):
    os.makedirs(log_dir, exist_ok=True)
    checkpoint = os.path.join(log_dir, 'weights.h5')

    model.save_weights(checkpoint)

    return keras.callbacks.ModelCheckpoint(checkpoint, save_weights_only=True)
